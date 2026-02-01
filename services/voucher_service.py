import random
import string
import logging
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
from pathlib import Path

from config import Config
from models.schemas import Voucher
from utils.helpers import generate_voucher_code, calculate_expiry_time
from utils.validators import (
    validate_voucher_code,
    validate_profile_name,
    validate_quantity,
    validate_customer_info,
)


logger = logging.getLogger(__name__)

try:
    from reportlab.lib.pagesizes import A4, letter, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch, cm
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate,
        Paragraph,
        Table,
        TableStyle,
        Spacer,
        Image,
        PageBreak,
    )
    from reportlab.pdfgen import canvas
    from reportlab.lib.enums import TA_CENTER, TA_LEFT

    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False
    logger.warning(
        "PDF generation libraries not available. Install reportlab for PDF support."
    )


class VoucherService:
    def __init__(self, config: Config, database_service, mikrotik_manager):
        self.config = config
        self.db = database_service
        self.mikrotik = mikrotik_manager
        self.pdf_output_dir = (
            Path(config.PDF_OUTPUT_DIR)
            if hasattr(config, "PDF_OUTPUT_DIR")
            else Path("pdf_vouchers")
        )
        self.pdf_output_dir.mkdir(exist_ok=True)

    def generate_voucher_code(self, uptime_limit: str) -> str:
        """Generate unique voucher code based on uptime limit"""
        config = self.config.VOUCHER_CONFIG.get(
            uptime_limit, self.config.VOUCHER_CONFIG["1d"]
        )

        while True:
            code = generate_voucher_code(config["length"], config["chars"])

            # Check if code already exists in database
            result = self.db.get_voucher(code)
            if not result:
                return code

    def create_vouchers(
        self,
        profile_name: str,
        quantity: int,
        customer_name: str = "",
        customer_contact: str = "",
        password_type: str = "blank",
        generate_pdf: bool = False,
    ) -> Tuple[bool, List[Dict[str, Any]], str]:
        """Create multiple vouchers with parallel execution for speed"""
        from concurrent.futures import ThreadPoolExecutor
        import time

        # Validate inputs
        is_valid, error = validate_profile_name(profile_name)
        if not is_valid:
            return False, [], error

        is_valid, error = validate_quantity(quantity)
        if not is_valid:
            return False, [], error

        is_valid, error = validate_customer_info(customer_name, customer_contact)
        if not is_valid:
            return False, [], error

        # Get profile information
        db_profile = self.db.get_profile(profile_name)
        if not db_profile:
            return False, [], "Profile not found"

        uptime_limit = db_profile.get("uptime_limit", "1d")
        price_per_voucher = db_profile.get("price", 1000)
        validity_period = db_profile.get("validity_period", 24)

        logger.info(f"Generating {quantity} vouchers for profile {profile_name}")

        codes = []
        for _ in range(quantity):
            codes.append(self.generate_voucher_code(uptime_limit))

        vouchers_data = []
        vouchers_to_db = []
        
        # Parallel MikroTik Creation
        def create_on_mikrotik(code):
            password = self._determine_password(password_type, code)
            comment = self._create_user_comment(
                customer_name, customer_contact, password_type
            )
            success = self.mikrotik.create_voucher(
                profile_name, code, password, comment, uptime_limit
            )
            if success:
                expiry = calculate_expiry_time(validity_period)
                created = datetime.now()
                return {
                    "code": code,
                    "password": self._get_password_display(password_type, password),
                    "profile": profile_name,
                    "uptime_limit": uptime_limit,
                    "customer_name": customer_name,
                    "customer_contact": customer_contact,
                    "expiry_time": expiry,
                    "created_at": created,
                    "password_type": password_type,
                    "price": price_per_voucher
                }
            return None

        # Increase workers for big tasks
        max_workers = min(10, quantity)
        creation_results = []
        try:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                creation_results = list(executor.map(create_on_mikrotik, codes))
        except Exception as e:
            logger.error(f"Critical error during parallel voucher creation: {e}")
            # If everything failed, we still have successful_vouchers check below

        # Filter successes
        successful_vouchers = [v for v in creation_results if v is not None]
        
        if not successful_vouchers:
            return False, [], "Failed to create any vouchers on MikroTik"

        # Batch Database Insertion
        db_vouchers = [
            Voucher(
                voucher_code=v["code"],
                profile_name=v["profile"],
                customer_name=v["customer_name"],
                customer_contact=v["customer_contact"],
                expiry_time=v["expiry_time"],
                uptime_limit=v["uptime_limit"],
                password_type=v["password_type"],
                created_at=v["created_at"]
            )
            for v in successful_vouchers
        ]
        
        if self.db.add_vouchers_batch(db_vouchers):
            vouchers_data = successful_vouchers
        else:
            logger.error("Failed to add vouchers to database after MikroTik creation")
            # Note: At this point, vouchers are on MikroTik but not in DB. 
            # In a perfectly robust system, we would attempt rollback on MikroTik.
            return False, [], "Failed to record vouchers in database"

        # Parallel PDF Generation if requested
        if generate_pdf and PDF_AVAILABLE:
            def gen_pdf(v):
                pdf_path = self.generate_single_voucher_pdf(v)
                if pdf_path:
                    v["pdf_path"] = pdf_path
                return v

            with ThreadPoolExecutor(max_workers=min(5, len(vouchers_data))) as executor:
                vouchers_data = list(executor.map(gen_pdf, vouchers_data))

            # Batch PDF
            if len(vouchers_data) > 1:
                batch_pdf_path = self.generate_batch_vouchers_pdf(
                    vouchers_data, profile_name, customer_name
                )
                if batch_pdf_path:
                    for v in vouchers_data:
                        v["batch_pdf_path"] = batch_pdf_path

        successful_creations = len(vouchers_data)
        message = f"Successfully created {successful_creations} out of {quantity} vouchers"
        if successful_creations < quantity:
            message += f". {quantity - successful_creations} failed on MikroTik."

        return True, vouchers_data, message

    def generate_single_voucher_pdf(
        self, voucher_data: Dict[str, Any]
    ) -> Optional[str]:
        """Generate a professionally designed PDF for a single voucher."""
        try:
            if not PDF_AVAILABLE:
                logger.warning("PDF generation not available")
                return None

            # Filename and path
            code_for_filename = voucher_data.get("code") or voucher_data.get("voucher_code") or "UNKNOWN"
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"voucher_{code_for_filename}_{timestamp}.pdf"
            filepath = self.pdf_output_dir / filename

            # Document setup
            doc = SimpleDocTemplate(
                str(filepath),
                pagesize=A4,
                topMargin=0.75 * inch,
                bottomMargin=0.75 * inch,
                leftMargin=1 * inch,
                rightMargin=1 * inch,
            )

            elements = []
            styles = getSampleStyleSheet()

            # Custom Styles
            header_style = ParagraphStyle(
                "HeaderStyle",
                parent=styles["Heading1"],
                fontSize=22,
                alignment=TA_CENTER,
                textColor=colors.white,
                backColor=colors.darkblue,
                borderPadding=10,
                spaceAfter=20,
            )
            
            hero_code_style = ParagraphStyle(
                "HeroCodeStyle",
                parent=styles["Heading1"],
                fontSize=36,
                alignment=TA_CENTER,
                textColor=colors.red,
                spaceBefore=20,
                spaceAfter=20,
            )

            label_style = ParagraphStyle(
                "LabelStyle",
                parent=styles["Normal"],
                fontSize=10,
                textColor=colors.grey,
                alignment=TA_LEFT,
            )
            
            value_style = ParagraphStyle(
                "ValueStyle",
                parent=styles["Normal"],
                fontSize=12,
                textColor=colors.black,
                alignment=TA_LEFT,
                fontName="Helvetica-Bold",
            )

            instr_title_style = ParagraphStyle(
                "InstrTitle",
                parent=styles["Heading2"],
                fontSize=14,
                spaceBefore=30,
                spaceAfter=10,
                textColor=colors.darkblue,
            )

            # Header
            elements.append(Paragraph("WIFI ACCESS VOUCHER", header_style))
            elements.append(Spacer(1, 0.4 * inch))

            # Hero Section (Code)
            elements.append(Paragraph("YOUR VOUCHER CODE", label_style))
            elements.append(Paragraph(voucher_data.get('code', code_for_filename), hero_code_style))
            elements.append(Spacer(1, 0.2 * inch))

            # Details Setup
            profile = voucher_data.get("profile") or voucher_data.get("profile_name") or "N/A"
            uptime_limit = voucher_data.get("uptime_limit") or voucher_data.get("limit") or "N/A"
            
            # Price handling
            price_val = voucher_data.get("price", 0)
            currency = voucher_data.get("currency", "UGX")
            price_str = f"{price_val:,.0f} {currency}" if price_val else "N/A"

            # Details Table (Clean layout)
            data = [
                [Paragraph("<b>Duration</b>", label_style), Paragraph("<b>Price</b>", label_style)],
                [Paragraph(uptime_limit, value_style), Paragraph(price_str, value_style)],
                [Spacer(1, 0.1 * inch), Spacer(1, 0.1 * inch)],
                [Paragraph("<b>Profile</b>", label_style), Paragraph("<b>Package Details</b>", label_style)],
                [Paragraph(profile, value_style), Paragraph("High Speed Internet Access", value_style)],
            ]

            table = Table(data, colWidths=[2.5 * inch, 2.5 * inch])
            table.setStyle(TableStyle([
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('LINEBELOW', (0, 1), (-1, 1), 0.5, colors.lightgrey),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ]))
            elements.append(table)

            # Instructions
            elements.append(Paragraph("HOW TO CONNECT", instr_title_style))
            
            step_style = ParagraphStyle("StepStyle", parent=styles["Normal"], fontSize=11, spaceAfter=8, bulletIndent=10)
            steps = [
                "1. Connect to the <b>WiFi Hotspot</b> network on your device.",
                "2. The login page should open automatically (or visit any website).",
                "3. Enter your unique <b>Voucher Code</b> shown above.",
                "4. Click <b>Login</b> to start enjoying your internet session!"
            ]
            for step in steps:
                elements.append(Paragraph(step, step_style))

            # Footer
            elements.append(Spacer(1, 1 * inch))
            footer_data = [
                [Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", label_style),
                 Paragraph("Thank you for using our service!", label_style)]
            ]
            footer_table = Table(footer_data, colWidths=[3 * inch, 3 * inch])
            footer_table.setStyle(TableStyle([
                ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
                ('TOPPADDING', (0, 0), (-1, -1), 10),
                ('LINEABOVE', (0, 0), (-1, 0), 0.5, colors.grey),
            ]))
            elements.append(footer_table)

            # Build PDF
            doc.build(elements)
            logger.info(f"Redesigned Single PDF generated: {filepath}")
            return str(filepath)

        except Exception as e:
            logger.error(f"Error generating redesigned PDF: {e}")
            return None


    def generate_batch_vouchers_pdf(
        self, vouchers: List[Dict[str, Any]], profile_name: str, customer_name: str = ""
    ) -> Optional[str]:
        """Generate a PDF with multiple vouchers (for batch printing)"""
        try:
            if not PDF_AVAILABLE:
                return None

            import re

            sanitized_profile = re.sub(r'[<>:"/\\|?*:]', "_", profile_name)
            sanitized_profile = sanitized_profile.replace(" ", "_")

            filename = f"batch_vouchers_{sanitized_profile}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
            filepath = self.pdf_output_dir / filename

            doc = SimpleDocTemplate(str(filepath), pagesize=A4)
            elements = []
            styles = getSampleStyleSheet()

            # Title
            title_style = ParagraphStyle(
                "BatchTitle",
                parent=styles["Heading1"],
                fontSize=14,
                spaceAfter=10,
                alignment=TA_CENTER,
            )

            elements.append(Paragraph(f"BATCH VOUCHERS - {profile_name}", title_style))

            if customer_name:
                elements.append(
                    Paragraph(f"Customer: {customer_name}", styles["Normal"])
                )

            elements.append(
                Paragraph(f"Total Vouchers: {len(vouchers)}", styles["Normal"])
            )
            elements.append(
                Paragraph(
                    f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    styles["Normal"],
                )
            )

            cutting_style = ParagraphStyle(
                "CuttingStyle",
                parent=styles["Normal"],
                fontSize=7,
                textColor=colors.grey,
                alignment=TA_CENTER,
            )
            
            elements.append(Paragraph("┌─ Cut along dotted lines ─┐", cutting_style))

            elements.append(Spacer(1, 0.3 * inch))

            vouchers_per_page = 32
            columns = 4
            rows = 8

            for page_num, i in enumerate(range(0, len(vouchers), vouchers_per_page)):
                page_vouchers = vouchers[i : i + vouchers_per_page]

                if page_num > 0:
                    # Add page break for subsequent pages
                    elements.append(PageBreak())
                    
                    elements.append(Paragraph("┌─ Cut along dotted lines ─┐", cutting_style))

                    elements.append(Spacer(1, 0.2 * inch))

                # FIX: This grid creation should be INSIDE the page loop
                grid_data = []
                for row in range(rows):
                    grid_row = []
                    for col in range(columns):
                        voucher_index = row * columns + col
                        if voucher_index < len(page_vouchers):
                            voucher = page_vouchers[voucher_index]
                            grid_row.append(self._create_voucher_card(voucher))
                        else:
                            # Empty cell
                            grid_row.append("")
                    grid_data.append(grid_row)

                grid_table = Table(grid_data, colWidths=[2.0 * inch] * columns,
                rowHeights=[0.9 * inch] * rows)
                grid_table.setStyle(
                    TableStyle(
                        [
                        # Outer border (light for cutting reference)
                        ("BOX", (0, 0), (-1, -1), 0.5, colors.lightgrey),
                        
                        # Inner grid with spacing for cutting
                        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
                        
                        # Cell spacing for cutting
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 6),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                        ("TOPPADDING", (0, 0), (-1, -1), 8),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                    ]
                    )
                )

                elements.append(grid_table)
                
                if i + vouchers_per_page < len(vouchers):
                    elements.append(Spacer(1, 0.1 * inch))
                    elements.append(Paragraph("▼ Cut here for next page ▼", cutting_style))

            elements.append(Spacer(1, 0.2 * inch))
            elements.append(Paragraph("✄ ── Cut along dotted lines ── ✄", cutting_style))

            # FIX: Moved doc.build outside the loop but inside the try block
            doc.build(elements)
            logger.info(f"Batch PDF generated: {filepath}")
            return str(filepath)

        except Exception as e:
            logger.error(f"Error generating batch PDF: {e}")
            return None

    def _create_voucher_card(self, voucher: Dict[str, Any]) -> Paragraph:
        """Create a professional, high-contrast voucher card for batch printing."""
        try:
            # Data normalization
            code = voucher.get("code") or voucher.get("voucher_code", "N/A")
            limit = voucher.get("uptime_limit") or voucher.get("limit") or "N/A"
            
            # Formatted content using ReportLab-friendly HTML-like tags
            # We use a clean, bold layout without ASCII boxes
            card_html = f"""
            <para align="center">
                <b><font size="12" color="darkblue">WIFI ACCESS</font></b><br/>
                <font size="14" color="red"><b>{code}</b></font><br/>
                <font size="8" color="black"><b>Duration:</b> {limit}</font><br/>
                <font size="7" color="grey"><i>Use at any hotspot location</i></font>
            </para>
            """

            card_style = ParagraphStyle(
                "BatchCardStyle",
                parent=getSampleStyleSheet()["Normal"],
                alignment=TA_CENTER,
                leading=12,
                borderPadding=5,
            )

            return Paragraph(card_html, card_style)

        except Exception as e:
            logger.error(f"Error creating batch voucher card: {e}")
            return Paragraph("Error", getSampleStyleSheet()["Normal"])


    def generate_voucher_card_pdf(self, voucher_data: Dict[str, Any]) -> Optional[str]:
        """Generate a premium, fancy landscape-style voucher card."""
        try:
            if not PDF_AVAILABLE:
                return None

            filename = f"voucher_card_{voucher_data.get('code', 'UNKNOWN')}.pdf"
            filepath = self.pdf_output_dir / filename

            c = canvas.Canvas(str(filepath), pagesize=landscape(letter))
            width, height = landscape(letter)

            # Elegant Background Gradient (Simulated with layers)
            c.setFillColor(colors.HexColor("#F0F4F8"))
            c.rect(0, 0, width, height, fill=1)
            
            # Highlight Bar
            c.setFillColor(colors.darkblue)
            c.rect(0, height - 100, width, 100, fill=1)

            # Border
            c.setStrokeColor(colors.darkblue)
            c.setLineWidth(2)
            c.rect(40, 40, width - 80, height - 80, stroke=1, fill=0)

            # Title - High visibility
            c.setFillColor(colors.white)
            c.setFont("Helvetica-Bold", 30)
            c.drawCentredString(width / 2, height - 65, "WIFI ACCESS VOUCHER")

            # Main Code Display
            c.setFillColor(colors.black)
            c.setFont("Helvetica", 14)
            c.drawCentredString(width / 2, height - 160, "YOUR ACCESS CODE")
            
            c.setFillColor(colors.red)
            c.setFont("Helvetica-Bold", 60)
            c.drawCentredString(width / 2, height - 230, voucher_data.get("code", "N/A"))

            # Bottom Info Bar
            c.setFillColor(colors.darkblue)
            c.rect(100, 100, width - 200, 60, fill=1)
            
            c.setFillColor(colors.white)
            c.setFont("Helvetica-Bold", 18)
            limit = voucher_data.get("uptime_limit") or "Unlimited"
            c.drawCentredString(width / 2, 125, f"VALIDITY: {limit}")

            # Instructions - Modern floating style
            c.setFillColor(colors.black)
            c.setFont("Helvetica-Oblique", 11)
            c.drawCentredString(width / 2, 80, "Connect to Hotspot • Enter Code • Enjoy High Speed Internet")

            c.save()
            logger.info(f"Fancy Voucher Card generated: {filepath}")
            return str(filepath)

        except Exception as e:
            logger.error(f"Error generating fancy voucher card: {e}")
            return None


    def _determine_password(
        self, password_type: str, voucher_code: str
    ) -> Optional[str]:
        """Determine password based on password type"""
        if password_type == "same":
            return "same"
        elif password_type == "custom":
            return generate_voucher_code(8, string.ascii_uppercase + string.digits)
        else:  # blank
            return None

    def _get_password_display(self, password_type: str, password: Optional[str]) -> str:
        """Get password display for response"""
        if password_type == "custom" and password:
            return password
        elif password_type == "same":
            return "same as username"
        else:
            return "blank"

    def _create_user_comment(
        self, customer_name: str, customer_contact: str, password_type: str
    ) -> str:
        """Create comment for MikroTik user"""
        comment_parts = ["Type: voucher"]
        if customer_name:
            comment_parts.append(f"Customer: {customer_name}")
        if customer_contact:
            comment_parts.append(f"Contact: {customer_contact}")
        if password_type != "blank":
            comment_parts.append(f"Password: {password_type}")

        return " | ".join(comment_parts)

    def get_voucher_info(
        self, voucher_code: str
    ) -> Tuple[bool, Optional[Dict[str, Any]], str]:
        """Get detailed voucher information"""
        is_valid, error = validate_voucher_code(voucher_code)
        if not is_valid:
            return False, None, error

        result = self.db.get_voucher(voucher_code)
        if not result:
            return False, None, "Voucher not found"

        usage = self.mikrotik.get_user_usage(voucher_code)
        profile_info = self.db.get_profile(result["profile_name"])
        price = profile_info.get("price", 1000) if profile_info else 1000

        voucher_info = {
            "code": result["voucher_code"],
            "profile_name": result["profile_name"],
            "created_at": result["created_at"],
            "activated_at": result["activated_at"],
            "is_used": bool(result["is_used"]),
            "bytes_used": result["bytes_used"],
            "session_time": result["session_time"],
            "customer_name": result["customer_name"],
            "customer_contact": result["customer_contact"],
            "uptime_limit": result["uptime_limit"],
            "password_type": result["password_type"],
            "current_usage": usage,
            "price": price,
        }

        return True, voucher_info, "Voucher found"

    def get_expired_vouchers(self) -> List[Dict[str, Any]]:
        """Get vouchers that have reached their uptime limit"""
        rows = (
            self.db.execute_query(
                """
            SELECT voucher_code, profile_name, activated_at, uptime_limit, is_expired
            FROM vouchers 
            WHERE is_used = TRUE
            ORDER BY activated_at DESC
            LIMIT 50
            """,
                fetch=True,
            )
            or []
        )

        expired_vouchers = []
        for row in rows:
            voucher_code = row["voucher_code"]
            uptime_limit = row["uptime_limit"]

            # Get current usage from MikroTik
            usage = self.mikrotik.get_user_usage(voucher_code)
            current_uptime = usage.get("uptime", "0s") if usage else "0s"

            # Check if uptime limit is reached
            from utils.helpers import check_uptime_limit

            is_expired = check_uptime_limit(current_uptime, uptime_limit)

            expired_vouchers.append(
                {
                    "voucher_code": voucher_code,
                    "profile_name": row["profile_name"],
                    "activated_at": row["activated_at"],
                    "uptime_limit": uptime_limit,
                    "current_uptime": current_uptime,
                    "is_expired": is_expired or bool(row["is_expired"]),
                }
            )

        return expired_vouchers
