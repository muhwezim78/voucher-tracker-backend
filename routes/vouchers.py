from flask import Blueprint, request, jsonify, abort, send_file
from typing import Dict, Any
import os
from pathlib import Path

from services.voucher_service import VoucherService

vouchers_bp = Blueprint("vouchers", __name__)


def init_vouchers_routes(app, voucher_service: VoucherService):
    """Initialize voucher routes with the service"""

    @vouchers_bp.route("/vouchers/generate", methods=["POST"])
    def generate_vouchers():
        data = request.json
        profile_name = data.get("profile_name")
        quantity = data.get("quantity", 1)
        customer_name = data.get("customer_name", "")
        customer_contact = data.get("customer_contact", "")
        password_type = data.get("password_type", "blank")
        generate_pdf = data.get("generate_pdf", False)

        success, vouchers, message = voucher_service.create_vouchers(
            profile_name,
            quantity,
            customer_name,
            customer_contact,
            password_type,
            generate_pdf,
        )

        if not success:
            return jsonify({"error": message}), 400

        total_price = sum(
            voucher_service.db.get_profile(voucher["profile"]).get("price", 1000)
            for voucher in vouchers
            if voucher_service.db.get_profile(voucher["profile"])
        )

        response_data = {
            "vouchers": vouchers,
            "message": message,
            "total_price": total_price,
        }

        if generate_pdf:
            pdf_vouchers = [v for v in vouchers if "pdf_path" in v]
            if pdf_vouchers:
                response_data["pdf_generated"] = True
                response_data["individual_pdfs"] = [
                    v["pdf_path"] for v in pdf_vouchers if "pdf_path" in v
                ]

                batch_pdfs = list(
                    set(
                        [v["batch_pdf_path"] for v in vouchers if "batch_pdf_path" in v]
                    )
                )
                if batch_pdfs:
                    response_data["batch_pdf"] = batch_pdfs[0]
            else:
                response_data["pdf_generated"] = False

        return jsonify(response_data)

    @vouchers_bp.route("/vouchers/<voucher_code>")
    def get_voucher_info(voucher_code):
        success, voucher_info, message = voucher_service.get_voucher_info(voucher_code)

        if not success:
            abort(404, description=message)

        return jsonify(voucher_info)

    @vouchers_bp.route("/vouchers/expired")
    def get_expired_vouchers_endpoint():
        try:
            expired_vouchers = voucher_service.get_expired_vouchers()
            return jsonify({"expired_vouchers": expired_vouchers})
        except Exception as e:
            return jsonify({"expired_vouchers": [], "error": str(e)}), 500

    @vouchers_bp.route("/vouchers/<voucher_code>/pdf", methods=["GET", "POST"])
    def generate_voucher_pdf(voucher_code):
        """Generate PDF for a specific voucher"""
        success, voucher_info, message = voucher_service.get_voucher_info(voucher_code)

        if not success:
            abort(404, description=message)

        # Determine PDF style from query parameter
        pdf_style = request.args.get("style", "standard")  # standard, card, batch

        try:
            if pdf_style == "card":
                pdf_path = voucher_service.generate_voucher_card_pdf(voucher_info)
            else:
                pdf_path = voucher_service.generate_single_voucher_pdf(voucher_info)

            if not pdf_path or not os.path.exists(pdf_path):
                return jsonify({"error": "PDF generation failed"}), 500

            # Return PDF file or path based on request
            if request.args.get("download", "true").lower() == "true":
                filename = f"voucher_{voucher_code}.pdf"
                return send_file(
                    pdf_path,
                    as_attachment=True,
                    download_name=filename,
                    mimetype="application/pdf",
                )
            else:
                return jsonify(
                    {
                        "message": "PDF generated successfully",
                        "pdf_path": pdf_path,
                        "voucher_code": voucher_code,
                    }
                )

        except Exception as e:
            return jsonify({"error": f"PDF generation failed: {str(e)}"}), 500

    @vouchers_bp.route("/vouchers/batch/pdf", methods=["POST"])
    def generate_batch_pdf():
        """Generate PDF for multiple vouchers"""
        data = request.json
        voucher_codes = data.get("voucher_codes", [])

        if not voucher_codes:
            return jsonify({"error": "No voucher codes provided"}), 400

        # Get voucher information for all codes
        vouchers_data = []
        for code in voucher_codes:
            success, voucher_info, message = voucher_service.get_voucher_info(code)
            if success:
                # NORMALIZE THE DATA STRUCTURE - Same as single voucher route
                normalized_voucher = {
                    "code": voucher_info.get("code")
                    or voucher_info.get("voucher_code"),
                    "profile": voucher_info.get("profile")
                    or voucher_info.get("profile_name"),
                    "uptime_limit": voucher_info.get("uptime_limit"),
                    "password_type": voucher_info.get("password_type", "blank"),
                    "expiry_time": voucher_info.get("expiry_time"),
                    "customer_name": voucher_info.get("customer_name", ""),
                    "customer_contact": voucher_info.get("customer_contact", ""),
                    "price": voucher_info.get("price", 0),
                    "is_used": voucher_info.get("is_used", False),
                }

                # Determine password display based on password type
                password_type = normalized_voucher["password_type"]
                if password_type == "same":
                    normalized_voucher["password"] = "same as username"
                elif password_type == "custom":
                    normalized_voucher["password"] = "custom password"
            else:  # blank
                normalized_voucher["password"] = "blank"

            vouchers_data.append(normalized_voucher)

        if not vouchers_data:
            return jsonify({"error": "No valid vouchers found"}), 404

        try:
            # Use the first voucher's profile for batch naming
            profile_name = vouchers_data[0].get(
                "profile", "batch"
            )  # Changed to "profile"
            customer_name = vouchers_data[0].get("customer_name", "")

            pdf_path = voucher_service.generate_batch_vouchers_pdf(
                vouchers_data, profile_name, customer_name
            )

            if not pdf_path or not os.path.exists(pdf_path):
                return jsonify({"error": "Batch PDF generation failed"}), 500

            if request.args.get("download", "true").lower() == "true":
                filename = f"batch_vouchers_{len(vouchers_data)}_{profile_name}.pdf"
                return send_file(
                    pdf_path,
                    as_attachment=True,
                    download_name=filename,
                    mimetype="application/pdf",
                )
            else:
                return jsonify(
                    {
                        "message": "Batch PDF generated successfully",
                        "pdf_path": pdf_path,
                        "voucher_count": len(vouchers_data),
                        "profile": profile_name,
                    }
                )

        except Exception as e:
            return jsonify({"error": f"Batch PDF generation failed: {str(e)}"}), 500

    @vouchers_bp.route("/vouchers/pdf/list")
    def list_generated_pdfs():
        """List all generated PDF files"""
        try:
            pdf_dir = voucher_service.pdf_output_dir
            pdf_files = []

            if pdf_dir.exists():
                for pdf_file in pdf_dir.glob("*.pdf"):
                    pdf_files.append(
                        {
                            "filename": pdf_file.name,
                            "path": str(pdf_file),
                            "size": pdf_file.stat().st_size,
                            "created": pdf_file.stat().st_ctime,
                        }
                    )

            return jsonify(
                {
                    "pdf_files": sorted(
                        pdf_files, key=lambda x: x["created"], reverse=True
                    ),
                    "total_count": len(pdf_files),
                }
            )
        except Exception as e:
            return jsonify({"error": f"Failed to list PDFs: {str(e)}"}), 500

    @vouchers_bp.route("/vouchers/pdf/cleanup", methods=["POST"])
    def cleanup_pdfs():
        """Clean up generated PDF files (optional)"""
        try:
            data = request.json or {}
            older_than_days = data.get(
                "older_than_days", 7
            )  # Default: clean up files older than 7 days

            pdf_dir = voucher_service.pdf_output_dir
            deleted_files = []

            if pdf_dir.exists():
                import time

                current_time = time.time()
                cutoff_time = current_time - (older_than_days * 24 * 60 * 60)

                for pdf_file in pdf_dir.glob("*.pdf"):
                    if pdf_file.stat().st_ctime < cutoff_time:
                        pdf_file.unlink()
                        deleted_files.append(pdf_file.name)

            return jsonify(
                {
                    "message": f"Cleaned up {len(deleted_files)} PDF files",
                    "deleted_files": deleted_files,
                }
            )
        except Exception as e:
            return jsonify({"error": f"PDF cleanup failed: {str(e)}"}), 500

    # Register blueprint
    app.register_blueprint(vouchers_bp)
