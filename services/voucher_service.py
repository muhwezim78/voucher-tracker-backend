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
        """Create multiple vouchers"""
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

        vouchers = []
        total_price = 0
        successful_creations = 0
        pdf_paths = []

        for i in range(quantity):
            try:
                voucher_code = self.generate_voucher_code(uptime_limit)

                # Create voucher in database
                voucher = Voucher(
                    voucher_code=voucher_code,
                    profile_name=profile_name,
                    customer_name=customer_name,
                    customer_contact=customer_contact,
                    expiry_time=calculate_expiry_time(validity_period),
                    uptime_limit=uptime_limit,
                    password_type=password_type,
                    created_at=datetime.now(),
                )

                if not self.db.add_voucher(voucher):
                    continue

                # Create voucher on MikroTik
                password = self._determine_password(password_type, voucher_code)
                comment = self._create_user_comment(
                    customer_name, customer_contact, password_type
                )

                success = self.mikrotik.create_voucher(
                    profile_name, voucher_code, password, comment, uptime_limit
                )

                if success:
                    password_display = self._get_password_display(
                        password_type, password
                    )
                    voucher_data = {
                        "code": voucher_code,
                        "password": password_display,
                        "profile": profile_name,
                        "uptime_limit": uptime_limit,
                        "customer_name": customer_name,
                        "customer_contact": customer_contact,
                        "expiry_time": voucher.expiry_time,
                        "created_at": voucher.created_at,
                        "price": price_per_voucher,
                    }
                    vouchers.append(voucher_data)
                    total_price += price_per_voucher
                    successful_creations += 1

                    if generate_pdf and PDF_AVAILABLE:
                        pdf_path = self.generate_single_voucher_pdf(voucher_data)
                        if pdf_path:
                            pdf_paths.append(pdf_path)
                            voucher_data["pdf_path"] = pdf_path

                else:
                    logger.error(f"Failed to create voucher {voucher_code} on MikroTik")

            except Exception as e:
                logger.error(f"Error creating voucher {i+1}: {e}")
                continue
        if generate_pdf and PDF_AVAILABLE and len(vouchers) > 1:
            batch_pdf_path = self.generate_batch_vouchers_pdf(
                vouchers, profile_name, customer_name
            )
            if batch_pdf_path:
                for voucher in vouchers:
                    voucher["batch_pdf_path"] = batch_pdf_path

        if successful_creations == 0:
            return False, [], "Failed to create any vouchers"

        message = (
            f"Successfully created {successful_creations} out of {quantity} vouchers"
        )
        if successful_creations < quantity:
            message += f". {quantity - successful_creations} failed."

        return True, vouchers, message

    def generate_single_voucher_pdf(
        self, voucher_data: Dict[str, Any]
    ) -> Optional[str]:
        """Generate a PDF for a single voucher.

        Improvements in this version:
        - Robustly reads profile name from 'profile' or 'profile_name'.
        - Uses price fields correctly:
            * If `price_cents` present -> price = price_cents / 100 with currency (default "$")
            * elif `price` present -> treat as already in major units (default currency "UGX")
            * falls back to 0 if missing.
          (This avoids accidental division by 100 on already-major-unit prices.)
        - Accepts expiry from several common keys and string formats; falls back to "N/A".
        - Safe lookups everywhere to avoid KeyError.
        """
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
                topMargin=0.5 * inch,
                bottomMargin=0.5 * inch,
                leftMargin=0.5 * inch,
                rightMargin=0.5 * inch,
            )

            elements = []
            styles = getSampleStyleSheet()

            # Styles
            title_style = ParagraphStyle(
                "CustomTitle",
                parent=styles["Heading1"],
                fontSize=18,
                spaceAfter=30,
                alignment=TA_CENTER,
                textColor=colors.darkblue,
            )
            content_style = ParagraphStyle(
                "CustomContent",
                parent=styles["Normal"],
                fontSize=12,
                spaceAfter=12,
                alignment=TA_LEFT,
            )
            code_style = ParagraphStyle(
                "CodeStyle",
                parent=styles["Heading1"],
                fontSize=24,
                spaceAfter=20,
                alignment=TA_CENTER,
                textColor=colors.red,
                backColor=colors.lightgrey,
            )

            # Title + Code
            elements.append(Paragraph("INTERNET ACCESS VOUCHER", title_style))
            elements.append(Spacer(1, 0.2 * inch))
            elements.append(Paragraph(f"CODE: {voucher_data.get('code', code_for_filename)}", code_style))
            elements.append(Spacer(1, 0.3 * inch))

            # Profile (robust)
            profile = (
                voucher_data.get("profile")
                or voucher_data.get("profile_name")
                or (voucher_data.get("profile_info") and voucher_data["profile_info"].get("name"))
                or "N/A"
            )

            # Price handling (avoid wrong division by 100 when price already in major units)
            currency = voucher_data.get("currency")
            price_value = None
            if "price_cents" in voucher_data and voucher_data["price_cents"] is not None:
                # explicit cents provided -> convert to major currency units
                try:
                    cents = float(voucher_data["price_cents"])
                    price_value = cents / 100.0
                    currency = currency or voucher_data.get("currency", "$")
                except Exception:
                    price_value = None
            elif "price" in voucher_data and voucher_data["price"] is not None:
                # assume price is already in major units (e.g., UGX, or dollars).
                try:
                    price_value = float(voucher_data["price"])
                    currency = currency or voucher_data.get("currency", "UGX")
                except Exception:
                    price_value = None
            else:
                # try nested profile price
                profile_info = voucher_data.get("profile_info") or {}
                if profile_info and ("price" in profile_info or "price_cents" in profile_info):
                    if "price_cents" in profile_info:
                        try:
                            price_value = float(profile_info["price_cents"]) / 100.0
                            currency = currency or profile_info.get("currency", "$")
                        except Exception:
                            price_value = None
                    else:
                        try:
                            price_value = float(profile_info.get("price", 0))
                            currency = currency or profile_info.get("currency", "UGX")
                        except Exception:
                            price_value = None

            # Format price string sensibly
            if price_value is None:
                price_str = "N/A"
            else:
                # if currency looks like a symbol, prefix; else suffix
                if currency in ("$", "€", "£"):
                    price_str = f"{currency}{price_value:,.2f}"
                else:
                    # Assume currency is a code like UGX, KES, etc.
                    # Show no decimals for large whole-unit currencies like UGX
                    if price_value == int(price_value) and price_value >= 1:
                        price_str = f"{int(price_value):,} {currency}"
                    else:
                        price_str = f"{price_value:,.2f} {currency}"

            # Password handling (display-friendly)
            # voucher may contain 'password', or a password_type & no password (blank/same)
            password_display = voucher_data.get("password")
            if not password_display:
                ptype = voucher_data.get("password_type", "blank")
                if ptype == "same":
                    password_display = "same as username"
                elif ptype == "custom":
                    # if type says custom but no password field present, indicate so
                    password_display = voucher_data.get("password", "custom (hidden)")
                else:
                    password_display = "blank"

            # Expiry handling (try multiple keys and formats)
            expiry_raw = (
                voucher_data.get("expiry_time")
                or voucher_data.get("expiry")
                or voucher_data.get("expires_at")
                or voucher_data.get("expiry_datetime")
                or voucher_data.get("valid_until")
            )
            expiry_str = "N/A"
            if expiry_raw:
                # If it's already a datetime-like object, format it
                if hasattr(expiry_raw, "strftime"):
                    try:
                        expiry_str = expiry_raw.strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        expiry_str = str(expiry_raw)
                else:
                    # If string, try ISO parse then common formats
                    if isinstance(expiry_raw, str):
                        parsed = None
                        try:
                            # try ISO first
                            parsed = datetime.fromisoformat(expiry_raw)
                        except Exception:
                            # try several common formats
                            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M"):
                                try:
                                    parsed = datetime.strptime(expiry_raw, fmt)
                                    break
                                except Exception:
                                    parsed = None
                        if parsed:
                            expiry_str = parsed.strftime("%Y-%m-%d %H:%M")
                        else:
                            expiry_str = expiry_raw  # fallback to raw string
                    else:
                        # Fallback: just convert to string
                        expiry_str = str(expiry_raw)

            # Uptime limit (safe)
            uptime_limit = voucher_data.get("uptime_limit") or voucher_data.get("limit") or "N/A"

            # Build details table
            details_data = [
                ["Profile:", profile],
                ["Uptime Limit:", uptime_limit],
                ["Password:", password_display],
                ["Expiry:", expiry_str],
                ["Price:", price_str],
            ]

            # Optional customer fields - keep top ordering
            if voucher_data.get("customer_name"):
                details_data.insert(0, ["Customer:", voucher_data.get("customer_name")])
            if voucher_data.get("customer_contact"):
                # if customer_name present, contact becomes second row; else first
                insert_pos = 1 if voucher_data.get("customer_name") else 0
                details_data.insert(insert_pos, ["Contact:", voucher_data.get("customer_contact")])

            table = Table(details_data, colWidths=[2 * inch, 3 * inch])
            table.setStyle(
                TableStyle(
                    [
                        ("FONT", (0, 0), (-1, -1), "Helvetica", 10),
                        ("BACKGROUND", (0, 0), (0, -1), colors.lightgrey),
                        ("ALIGN", (0, 0), (0, -1), "RIGHT"),
                        ("ALIGN", (1, 0), (1, -1), "LEFT"),
                        ("GRID", (0, 0), (-1, -1), 1, colors.black),
                        ("PADDING", (0, 0), (-1, -1), 6),
                    ]
                )
            )

            elements.append(table)
            elements.append(Spacer(1, 0.3 * inch))

            # Instructions
            instructions = [
                "INSTRUCTIONS:",
                "1. Connect to the WiFi network.",
                "2. Open your browser and go to the hotspot login page.",
                "3. Enter the voucher code and password.",
                "4. Click Login to start your session.",
            ]
            for instruction in instructions:
                elements.append(Paragraph(instruction, content_style))

            # Footer
            elements.append(Spacer(1, 0.5 * inch))
            gen_on = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            elements.append(Paragraph(f"Generated on: {gen_on}", content_style))

            # Build PDF
            doc.build(elements)
            logger.info(f"PDF generated: {filepath}")
            return str(filepath)

        except Exception as e:
            logger.error(
                f"Error generating PDF for voucher {voucher_data.get('code', voucher_data.get('voucher_code', 'UNKNOWN'))}: {e}"
            )
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
        """Create a formatted voucher card for grid display"""
        try:
            # Get voucher data with safe access
            voucher_code = voucher.get("code") or voucher.get("voucher_code", "N/A")
            profile = voucher.get("profile") or voucher.get("profile_name", "N/A")
            uptime_limit = voucher.get("uptime_limit", "N/A")

            # Handle password type
            password_type = voucher.get("password_type", "blank")
            password_display = "No Password"
            if password_type == "same":
                password_display = "Same as Username"
            elif password_type == "custom":
                password_display = "Custom Password"

            # Handle expiry time
            expiry_time = voucher.get("expiry_time", "N/A")
            if hasattr(expiry_time, "strftime"):
                expiry_display = expiry_time.strftime("%m/%d %H:%M")
            else:
                expiry_display = str(expiry_time)
                if len(expiry_display) > 10:
                    expiry_display = expiry_display[:10]

            # Create formatted voucher card content
            card_content = f"""
        <b><font size="9" color="darkblue">╔══════════════╗</font></b><br/>
        <b><font size="10">{voucher_code}</font></b><br/>
        <font size="6"><b>Profile:</b> {profile}</font><br/>
        <font size="6"><b>Limit:</b> {uptime_limit}</font><br/>
        <font size="6"><b>Password:</b> {password_display}</font><br/>
        <font size="6"><b>Expires:</b> {expiry_display}</font><br/>
        <b><font size="9" color="darkblue">╚══════════════╝</font></b>
            """

            # Create paragraph style for the voucher card
            card_style = ParagraphStyle(
                "VoucherCard",
                parent=getSampleStyleSheet()["Normal"],
                fontSize=6,
                leading=8,
                alignment=TA_CENTER,
                textColor=colors.black,
                borderPadding=4,
                leftIndent=0,
                rightIndent=0,
                spaceBefore=2,
                spaceAfter=2,
            )

            return Paragraph(card_content, card_style)

        except Exception as e:
            logger.error(f"Error creating voucher card: {e}")
            return Paragraph(
                "Error generating voucher", getSampleStyleSheet()["Normal"]
            )

    def generate_voucher_card_pdf(self, voucher_data: Dict[str, Any]) -> Optional[str]:
        """Generate a fancy voucher card style PDF"""
        try:
            if not PDF_AVAILABLE:
                return None

            filename = f"voucher_card_{voucher_data['code']}.pdf"
            filepath = self.pdf_output_dir / filename

            # Create PDF with canvas for more control
            c = canvas.Canvas(str(filepath), pagesize=landscape(letter))
            width, height = landscape(letter)

            # Background
            c.setFillColor(colors.lightblue)
            c.rect(0, 0, width, height, fill=1)

            # Border
            c.setStrokeColor(colors.darkblue)
            c.setLineWidth(3)
            c.rect(20, 20, width - 40, height - 40, stroke=1, fill=0)

            # Title
            c.setFillColor(colors.darkblue)
            c.setFont("Helvetica-Bold", 24)
            c.drawCentredString(width / 2, height - 80, "INTERNET ACCESS VOUCHER")

            # Voucher Code (big and centered)
            c.setFillColor(colors.red)
            c.setFont("Helvetica-Bold", 32)
            c.drawCentredString(width / 2, height - 150, voucher_data["code"])

            # Details box
            c.setFillColor(colors.white)
            c.rect(50, height - 300, width - 100, 200, fill=1)
            c.setFillColor(colors.black)

            y_position = height - 120
            details = [
                ("Profile:", voucher_data["profile"]),
                ("Uptime Limit:", voucher_data["uptime_limit"]),
                ("Password:", voucher_data["password"]),
                ("Expiry:", voucher_data["expiry_time"].strftime("%Y-%m-%d %H:%M")),
            ]

            c.setFont("Helvetica-Bold", 14)
            for label, value in details:
                c.drawString(100, y_position, label)
                c.setFont("Helvetica", 14)
                c.drawString(250, y_position, str(value))
                c.setFont("Helvetica-Bold", 14)
                y_position -= 30

            # Instructions
            c.setFont("Helvetica", 10)
            instructions = [
                "Instructions: Connect to WiFi -> Open browser -> Enter code -> Enjoy!"
            ]

            y_position = 100
            for instruction in instructions:
                c.drawString(100, y_position, instruction)
                y_position -= 20

            c.save()
            return str(filepath)

        except Exception as e:
            logger.error(f"Error generating voucher card PDF: {e}")
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
