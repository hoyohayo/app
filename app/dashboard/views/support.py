import json
import urllib.parse
from typing import Union

import requests
from flask import render_template, request, flash, url_for, redirect, g
from flask_login import login_required, current_user
from werkzeug.datastructures import FileStorage

from app.dashboard.base import dashboard_bp
from app.extensions import limiter
from app.log import LOG
from app.models import Mailbox
from app.config import ZENDESK_HOST

VALID_MIME_TYPES = ["text/plain", "message/rfc822"]


@dashboard_bp.route("/support", methods=["GET"])
@login_required
def show_support_dialog():
    if not ZENDESK_HOST:
        flash("Support form is not enabled", "warning")
        return redirect(url_for("dashboard.index"))
    return render_template("dashboard/support.html", ticket_email=current_user.email)


def check_zendesk_response_status(response_code: int) -> bool:
    if response_code != 201:
        if response_code in (401 or 422):
            LOG.error("Could not authenticate")
        else:
            LOG.error("Problem with the request. Status {}".format(response_code))
        return False
    return True


def upload_file_to_zendesk_and_get_upload_token(file: FileStorage) -> Union[None, str]:
    if file.mimetype not in VALID_MIME_TYPES and not file.mimetype.startswith("image/"):
        flash(
            "File {} is not an image, text or an email".format(file.filename), "warning"
        )
        return
    escaped_filename = urllib.parse.urlencode({"filename": file.filename})
    url = "https://{}/api/v2/uploads?{}".format(ZENDESK_HOST, escaped_filename)
    headers = {"content-type": file.mimetype}
    response = requests.post(url, headers=headers, data=file.stream)
    if not check_zendesk_response_status(response.status_code):
        return
    data = response.json()
    return data["upload"]["token"]


def create_zendesk_request(email: str, content: str, files: [FileStorage]) -> bool:
    tokens = []
    for file in files:
        if not file.filename:
            continue
        token = upload_file_to_zendesk_and_get_upload_token(file)
        if token is None:
            return False
        tokens.append(token)
    data = {
        "request": {
            "subject": "Ticket created for user {}".format(current_user.id),
            "comment": {"type": "Comment", "body": content, "uploads": tokens},
            "requester": {
                "name": "SimpleLogin user {}".format(current_user.id),
                "email": email,
            },
        }
    }
    url = "https://{}/api/v2/requests.json".format(ZENDESK_HOST)
    headers = {"content-type": "application/json"}
    response = requests.post(url, data=json.dumps(data), headers=headers)
    if not check_zendesk_response_status(response.status_code):
        return False
    LOG.debug("Ticket created")
    return True


@dashboard_bp.route("/support", methods=["POST"])
@login_required
@limiter.limit(
    "2/hour", deduct_when=lambda r: hasattr(g, "deduct_limit") and g.deduct_limit
)
def process_support_dialog():
    if not ZENDESK_HOST:
        return render_template("dashboard/support_disabled.html")
    content = request.form.get("ticket_content") or ""
    email = request.form.get("ticket_email") or ""
    if not content:
        flash("Please add a description", "warning")
        return render_template("dashboard/support.html", ticket_email=email)
    if not email:
        flash("Please add an email", "warning")
        return render_template("dashboard/support.html", ticket_content=content)
    if not create_zendesk_request(
        email, content, request.files.getlist("ticket_files")
    ):
        return render_template(
            "dashboard/support.html", ticket_email=email, ticket_content=content
        )
    g.deduct_limit = True
    flash("Ticket created. You should have received an email notification.", "success")
    return redirect(url_for("dashboard.index"))
