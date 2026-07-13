# =============================================================================
# mcp_server.py — Production Remote MCP Server for Voicebot Tool Calling
# =============================================================================
# Stack  : FastMCP + Starlette + Uvicorn
# Mode   : Stateless HTTP (horizontal scaling safe)
# Auth   : x-api-key header
# Tools  : Customer lookup, Appointment booking, Order status,
#          Escalate to agent, Send SMS, Payment/billing info, Send Email
# =============================================================================

from __future__ import annotations

import asyncio
import logging
import os
import smtplib
import uuid
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import httpx
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from dotenv import load_dotenv
load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("mcp_server")

# ---------------------------------------------------------------------------
# Environment / config
# ---------------------------------------------------------------------------
MCP_API_KEY            = os.environ.get("MCP_API_KEY", "")
CRM_BASE_URL           = os.environ.get("CRM_BASE_URL", "http://localhost:9001")
BOOKING_BASE_URL       = os.environ.get("BOOKING_BASE_URL", "http://localhost:9002")
ORDER_BASE_URL         = os.environ.get("ORDER_BASE_URL", "http://localhost:9003")
SMS_BASE_URL           = os.environ.get("SMS_BASE_URL", "http://localhost:9004")
BILLING_BASE_URL       = os.environ.get("BILLING_BASE_URL", "http://localhost:9005")
ESCALATION_WEBHOOK_URL = os.environ.get("ESCALATION_WEBHOOK_URL", "")
SMTP_HOST              = os.environ.get("SMTP_HOST", "")
SMTP_PORT              = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER              = os.environ.get("SMTP_USER", "")
SMTP_PASS              = os.environ.get("SMTP_PASS", "")
SMTP_TIMEOUT = float(os.environ.get("SMTP_TIMEOUT", "15.0"))

# WhatsApp (Pinnacle / Pinbot gateway)
WHATSAPP_API_KEY      = os.environ.get("WHATSAPP_API_KEY", "f8780b41-131e-11f1-abfb-02c8a5e042bd")
WHATSAPP_PHONE_ID     = os.environ.get("WHATSAPP_PHONE_ID", "1047113271808861")
WHATSAPP_BASE_URL     = os.environ.get("WHATSAPP_BASE_URL", "https://partnersv1.pinbot.ai")


# How long (seconds) each upstream call may take before we give up
HTTP_TIMEOUT = float(os.environ.get("TOOL_HTTP_TIMEOUT", "4.0"))


# ---------------------------------------------------------------------------
# Shared async HTTP client (connection-pooled, reused across requests)
# ---------------------------------------------------------------------------
_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
    return _http_client


# ---------------------------------------------------------------------------
# Auth Middleware
# ---------------------------------------------------------------------------
class APIKeyMiddleware(BaseHTTPMiddleware):
    """Reject every request that does not carry the correct x-api-key header.
    The /health endpoint is always allowed through (used by load-balancer probes)."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path in ("/health", "/"):
            return await call_next(request)

        api_key = request.headers.get("x-api-key", "")
        if not MCP_API_KEY:
            logger.critical("MCP_API_KEY env var is not set — server is INSECURE")
        if api_key != MCP_API_KEY:
            logger.warning(
                "Unauthorized request from %s | path=%s",
                request.client.host if request.client else "unknown",
                request.url.path,
            )
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        return await call_next(request)


# ---------------------------------------------------------------------------
# MCP Server (stateless — safe for multi-worker / multi-replica deploys)
# ---------------------------------------------------------------------------
mcp = FastMCP("voicebot-tools")


# ===========================================================================
# TOOL 1 — Customer Lookup
# ===========================================================================
@mcp.tool()
async def get_customer_info(phone_number: str) -> str:
    """
    Fetch customer profile from the CRM by phone number.
    Returns name, customer ID, active plan, and account status.
    Call this first at the start of every conversation.

    Args:
        phone_number: Customer's phone number in E.164 format (e.g. +919876543210)
    """
    logger.info("Tool: get_customer_info | phone=%s", phone_number)
    try:
        client = get_http_client()
        r = await client.get(
            f"{CRM_BASE_URL}/api/customers/lookup",
            params={"phone": phone_number},
            headers={"x-internal-key": os.environ.get("INTERNAL_API_KEY", "")},
        )
        r.raise_for_status()
        d = r.json()
        return (
            f"Customer found — "
            f"Name: {d.get('name', 'N/A')}, "
            f"ID: {d.get('customer_id', 'N/A')}, "
            f"Plan: {d.get('plan', 'N/A')}, "
            f"Status: {d.get('status', 'N/A')}, "
            f"Language: {d.get('preferred_language', 'hi-IN')}"
        )
    except httpx.TimeoutException:
        logger.error("get_customer_info timeout | phone=%s", phone_number)
        return "CRM is taking too long to respond. Please ask the customer to hold for a moment."
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"No customer found for phone number {phone_number}. They may be a new customer."
        logger.error("get_customer_info HTTP error: %s", e)
        return "Unable to fetch customer details right now. Please proceed manually."
    except Exception as e:
        logger.exception("get_customer_info unexpected error")
        return "Customer lookup failed. Please ask the customer for their details directly."


# ===========================================================================
# TOOL 2 — Appointment Booking
# ===========================================================================
@mcp.tool()
async def book_appointment(
    customer_id: str,
    slot: str,
    service_type: str,
    notes: Optional[str] = None,
) -> str:
    """
    Book an appointment slot for a customer.
    Always confirm the slot with the customer before calling this tool.

    Args:
        customer_id : Customer's unique ID (get from get_customer_info first)
        slot        : Appointment datetime in ISO 8601 format (e.g. 2025-04-10T14:30:00)
        service_type: Type of service (e.g. 'technical_support', 'sales_demo', 'consultation')
        notes       : Optional additional notes about the appointment
    """
    logger.info(
        "Tool: book_appointment | customer=%s slot=%s service=%s",
        customer_id, slot, service_type,
    )
    try:
        client = get_http_client()
        r = await client.post(
            f"{BOOKING_BASE_URL}/api/appointments",
            json={
                "customer_id": customer_id,
                "slot": slot,
                "service_type": service_type,
                "notes": notes or "",
                "booked_via": "voicebot",
                "booking_ref": str(uuid.uuid4())[:8].upper(),
            },
            headers={"x-internal-key": os.environ.get("INTERNAL_API_KEY", "")},
        )
        r.raise_for_status()
        d = r.json()
        return (
            f"Appointment confirmed! "
            f"Booking ID: {d.get('booking_id', 'N/A')}, "
            f"Slot: {d.get('slot_display', slot)}, "
            f"Service: {service_type}. "
            f"A confirmation will be sent to the customer."
        )
    except httpx.TimeoutException:
        logger.error("book_appointment timeout | customer=%s", customer_id)
        return "Booking system is slow right now. Please try again in a moment."
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 409:
            return f"That slot ({slot}) is already taken. Please offer the customer an alternative time."
        logger.error("book_appointment HTTP error: %s", e)
        return "Could not complete the booking. Please try a different slot or try again."
    except Exception as e:
        logger.exception("book_appointment unexpected error")
        return "Booking failed due to a system error. Please note the details and book manually."


# ===========================================================================
# TOOL 3 — Order / Ticket Status
# ===========================================================================
@mcp.tool()
async def get_order_status(
    customer_id: str,
    order_id: Optional[str] = None,
) -> str:
    """
    Fetch the latest order or support ticket status for a customer.
    If order_id is not provided, returns the most recent order.

    Args:
        customer_id: Customer's unique ID
        order_id   : Optional specific order or ticket ID to look up
    """
    logger.info(
        "Tool: get_order_status | customer=%s order=%s",
        customer_id, order_id,
    )
    try:
        client = get_http_client()
        params: dict = {"customer_id": customer_id}
        if order_id:
            params["order_id"] = order_id

        r = await client.get(
            f"{ORDER_BASE_URL}/api/orders/status",
            params=params,
            headers={"x-internal-key": os.environ.get("INTERNAL_API_KEY", "")},
        )
        r.raise_for_status()
        d = r.json()
        return (
            f"Order ID: {d.get('order_id', 'N/A')}, "
            f"Status: {d.get('status', 'N/A')}, "
            f"Last Updated: {d.get('updated_at', 'N/A')}, "
            f"Details: {d.get('status_message', 'No additional details')}"
        )
    except httpx.TimeoutException:
        logger.error("get_order_status timeout | customer=%s", customer_id)
        return "Order system is not responding. Please ask the customer to check via app or website."
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"No order found for {'order ID ' + order_id if order_id else 'this customer'}."
        logger.error("get_order_status HTTP error: %s", e)
        return "Unable to fetch order status right now."
    except Exception as e:
        logger.exception("get_order_status unexpected error")
        return "Order status check failed. Please try again."


# ===========================================================================
# TOOL 4 — Escalate to Human Agent
# ===========================================================================
@mcp.tool()
async def escalate_to_agent(
    customer_id: str,
    reason: str,
    priority: str = "normal",
    call_id: Optional[str] = None,
) -> str:
    """
    Escalate the current call to a live human agent.
    Use this when: customer explicitly asks for a human, issue is too complex,
    customer is angry/distressed, or the voicebot cannot resolve the issue.

    Args:
        customer_id: Customer's unique ID
        reason     : Brief reason for escalation (shown to the agent)
        priority   : 'normal' | 'high' | 'urgent' — use 'urgent' for distressed customers
        call_id    : Current call session ID if available
    """
    logger.info(
        "Tool: escalate_to_agent | customer=%s priority=%s reason=%s",
        customer_id, priority, reason,
    )
    try:
        client = get_http_client()
        payload = {
            "customer_id": customer_id,
            "reason": reason,
            "priority": priority,
            "call_id": call_id or "",
            "escalated_at": datetime.utcnow().isoformat(),
            "source": "voicebot",
        }

        if ESCALATION_WEBHOOK_URL:
            r = await client.post(
                ESCALATION_WEBHOOK_URL,
                json=payload,
                headers={"x-internal-key": os.environ.get("INTERNAL_API_KEY", "")},
            )
            r.raise_for_status()
            d = r.json()
            wait_time = d.get("estimated_wait_seconds", 60)
            agent_name = d.get("agent_name", "the next available agent")
            return (
                f"Escalation successful. Transferring to {agent_name}. "
                f"Estimated wait: {wait_time // 60} minute(s). "
                f"Please inform the customer and stay on the line."
            )
        else:
            logger.warning("ESCALATION_WEBHOOK_URL not set — escalation logged only")
            return (
                "Escalation request logged. "
                "Please inform the customer that an agent will call them back within 30 minutes."
            )

    except httpx.TimeoutException:
        logger.error("escalate_to_agent timeout | customer=%s", customer_id)
        return (
            "Escalation system is slow. "
            "Please tell the customer an agent will call them back shortly."
        )
    except Exception as e:
        logger.exception("escalate_to_agent unexpected error")
        return (
            "Could not complete escalation automatically. "
            "Please manually transfer the call and note the customer's details."
        )


# ===========================================================================
# TOOL 5 — Send SMS / WhatsApp Notification
# ===========================================================================
@mcp.tool()
async def send_notification(
    phone_number: str,
    message: str,
    channel: str = "sms",
) -> str:
    """
    Send an SMS or WhatsApp message to the customer.
    Use this to send confirmation details, booking references,
    payment links, or follow-up information during or after a call.

    Args:
        phone_number: Customer phone in E.164 format (e.g. +919876543210)
        message     : Message content (keep under 160 chars for SMS)
        channel     : 'sms' or 'whatsapp'
    """
    logger.info(
        "Tool: send_notification | phone=%s channel=%s len=%d",
        phone_number, channel, len(message),
    )
    try:
        client = get_http_client()
        r = await client.post(
            f"{SMS_BASE_URL}/api/notify/send",
            json={
                "to": phone_number,
                "message": message,
                "channel": channel,
                "sent_via": "voicebot",
            },
            headers={"x-internal-key": os.environ.get("INTERNAL_API_KEY", "")},
        )
        r.raise_for_status()
        return f"{channel.upper()} sent successfully to {phone_number}."
    except httpx.TimeoutException:
        logger.error("send_notification timeout | phone=%s", phone_number)
        return f"Notification service timed out. The {channel.upper()} may not have been sent."
    except Exception as e:
        logger.exception("send_notification unexpected error")
        return f"Failed to send {channel.upper()}. Please manually share the information with the customer."


# ===========================================================================
# TOOL 6 — Payment / Billing Info
# ===========================================================================
@mcp.tool()
async def get_billing_info(
    customer_id: str,
    include_history: bool = False,
) -> str:
    """
    Fetch current billing status and payment information for a customer.
    Use this when customer asks about their bill, due date, or payment history.

    Args:
        customer_id    : Customer's unique ID
        include_history: Set True to include last 3 payment transactions
    """
    logger.info(
        "Tool: get_billing_info | customer=%s history=%s",
        customer_id, include_history,
    )
    try:
        client = get_http_client()
        r = await client.get(
            f"{BILLING_BASE_URL}/api/billing/summary",
            params={
                "customer_id": customer_id,
                "include_history": str(include_history).lower(),
            },
            headers={"x-internal-key": os.environ.get("INTERNAL_API_KEY", "")},
        )
        r.raise_for_status()
        d = r.json()

        summary = (
            f"Plan: {d.get('plan', 'N/A')}, "
            f"Amount Due: ₹{d.get('amount_due', 0)}, "
            f"Due Date: {d.get('due_date', 'N/A')}, "
            f"Payment Status: {d.get('payment_status', 'N/A')}"
        )

        if include_history and d.get("transactions"):
            txns = d["transactions"][:3]
            history = " | ".join(
                f"₹{t.get('amount')} on {t.get('date')} ({t.get('status')})"
                for t in txns
            )
            summary += f". Recent payments: {history}"

        return summary

    except httpx.TimeoutException:
        logger.error("get_billing_info timeout | customer=%s", customer_id)
        return "Billing system is slow. Please ask the customer to check the app for billing details."
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return "No billing information found for this customer."
        logger.error("get_billing_info HTTP error: %s", e)
        return "Unable to fetch billing information right now."
    except Exception as e:
        logger.exception("get_billing_info unexpected error")
        return "Billing lookup failed. Please transfer to billing department if needed."


# ===========================================================================
# TOOL 7 — Test Add (dev/debug only)
# ===========================================================================
@mcp.tool()
async def test_add(a: int, b: int) -> str:
    """Addition of two numbers."""
    return str(a + b)


# ===========================================================================
# TOOL 8 — Send Email
# ===========================================================================
def _send_smtp(msg: MIMEMultipart, to: str) -> None:
    """Blocking SMTP helper — called via run_in_executor to avoid blocking the event loop."""
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()   # required after STARTTLS for servers like Gmail
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, to, msg.as_string())


@mcp.tool()
async def send_email(
    to: str,
    subject: str,
    body: str,
    is_html: bool = False,
) -> str:
    """
    Send an email to a customer or internal team.
    Use this to send appointment confirmations, billing summaries,
    order updates, or any follow-up information via email.

    Args:
        to      : Recipient email address (e.g. customer@example.com)
        subject : Email subject line
        body    : Email body content (plain text or HTML)
        is_html : Set True if body contains HTML, False for plain text
    """
    logger.info("Tool: send_email | to=%s | subject=%s", to, subject)

    if not all([SMTP_HOST, SMTP_USER, SMTP_PASS]):
        logger.error("send_email: SMTP config missing (SMTP_HOST/SMTP_USER/SMTP_PASS not set)")
        return "Email service is not configured. Please contact support."

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SMTP_USER
        msg["To"]      = to

        mime_type = "html" if is_html else "plain"
        msg.attach(MIMEText(body, mime_type))

        loop = asyncio.get_running_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, _send_smtp, msg, to),
            timeout=SMTP_TIMEOUT + 5,
        )

        logger.info("send_email success | to=%s", to)
        return f"Email sent successfully to {to}."

    except asyncio.TimeoutError:
        logger.error("send_email: SMTP timed out after %.0fs | to=%s", SMTP_TIMEOUT, to)
        return "Email could not be sent — mail server took too long to respond. Please try again."
    except smtplib.SMTPAuthenticationError:
        logger.error("send_email: SMTP authentication failed | user=%s", SMTP_USER)
        return "Email authentication failed. Please check SMTP credentials."
    except smtplib.SMTPRecipientsRefused:
        logger.error("send_email: recipient refused | to=%s", to)
        return f"Email delivery failed — the address '{to}' was rejected by the mail server."
    except smtplib.SMTPException as e:
        logger.error("send_email: SMTP error | %s", e)
        return "Email could not be sent due to a mail server error. Please try again."
    except Exception as e:
        logger.exception("send_email unexpected error")
        return f"Failed to send email to {to}. Please try again or contact support."


# ===========================================================================
# TOOL 9 — Send WhatsApp Message (Pinnacle / Pinbot gateway)
# ===========================================================================
@mcp.tool()
async def send_whatsapp(
    to: str,
    message_type: str,
    text: Optional[str] = None,
    document_url: Optional[str] = None,
    document_caption: Optional[str] = None,
    image_url: Optional[str] = None,
    image_caption: Optional[str] = None,
) -> str:
    """
    Send a WhatsApp message to a customer via the Pinnacle/Pinbot gateway.
    Supports text messages, documents (PDF, etc.), and images.

    Args:
        to               : Customer's phone number — digits only, with country code
                           (e.g. '919876543210'). Leading zeros are stripped automatically.
        message_type     : One of 'text' | 'document' | 'image'
        text             : Message body — required when message_type is 'text'
        document_url     : Publicly accessible URL of the document — required for 'document'
        document_caption : Optional caption shown below the document
        image_url        : Publicly accessible URL of the image — required for 'image'
        image_caption    : Optional caption shown below the image
    """
    logger.info(
        "Tool: send_whatsapp | to=%s type=%s",
        to, message_type,
    )

    if not WHATSAPP_API_KEY or not WHATSAPP_PHONE_ID:
        logger.error("send_whatsapp: WHATSAPP_API_KEY or WHATSAPP_PHONE_ID not set")
        return "WhatsApp service is not configured. Please contact support."

    # Sanitise phone number — digits only, no stripping of country code
    clean_to = "".join(filter(str.isdigit, to))
    # If number looks like a local Indian number (10 digits starting with 6-9), prepend 91
    if len(clean_to) == 10 and clean_to[0] in "6789":
        clean_to = "91" + clean_to
        logger.info("send_whatsapp: prepended country code → %s", clean_to)

    if not clean_to:
        return "Invalid phone number provided. Please supply a valid number with country code."

    # Build payload based on message type
    message_type = message_type.lower().strip()

    if message_type == "text":
        if not text:
            return "Parameter 'text' is required when message_type is 'text'."
        payload = {
            "messaging_product": "whatsapp",
            "to": clean_to,
            "type": "text",
            "text": {"body": text},
        }

    elif message_type == "document":
        if not document_url:
            return "Parameter 'document_url' is required when message_type is 'document'."
        payload = {
            "messaging_product": "whatsapp",
            "to": clean_to,
            "type": "document",
            "document": {
                "link": document_url,
                "caption": document_caption or "",
            },
        }

    elif message_type == "image":
        if not image_url:
            return "Parameter 'image_url' is required when message_type is 'image'."
        payload = {
            "messaging_product": "whatsapp",
            "to": clean_to,
            "type": "image",
            "image": {
                "link": image_url,
                "caption": image_caption or "",
            },
        }

    else:
        return (
            f"Unsupported message_type '{message_type}'. "
            "Allowed values: 'text', 'document', 'image'."
        )

    try:
        client = get_http_client()
        url = f"{WHATSAPP_BASE_URL}/v3/{WHATSAPP_PHONE_ID}/messages"

        logger.info("send_whatsapp payload | %s", payload)

        r = await client.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "apikey": WHATSAPP_API_KEY,
            },
        )
        r.raise_for_status()
        d = r.json()

        msg_id = d.get("messages", [{}])[0].get("id", "N/A") if isinstance(d.get("messages"), list) else "N/A"
        logger.info("send_whatsapp success | to=%s msg_id=%s", clean_to, msg_id)
        return (
            f"WhatsApp {message_type} sent successfully to +{clean_to}. "
            f"Message ID: {msg_id}."
        )

    except httpx.TimeoutException:
        logger.error("send_whatsapp timeout | to=%s", clean_to)
        return "WhatsApp gateway timed out. The message may not have been delivered. Please try again."
    except httpx.HTTPStatusError as e:
        logger.error("send_whatsapp HTTP error %s | body=%s", e.response.status_code, e.response.text)
        if e.response.status_code == 401:
            return "WhatsApp authentication failed. Please check the API key."
        if e.response.status_code == 400:
            return f"WhatsApp rejected the request — please verify the phone number and payload. Detail: {e.response.text[:200]}"
        return f"WhatsApp delivery failed (HTTP {e.response.status_code}). Please try again."
    except Exception:
        logger.exception("send_whatsapp unexpected error")
        return "Failed to send WhatsApp message due to an unexpected error. Please try again."


# ---------------------------------------------------------------------------
# Mount MCP + Middleware into Starlette app
# ---------------------------------------------------------------------------
mcp_app = mcp.http_app(path="/mcp", stateless_http=True)

app = Starlette(
    routes=mcp_app.routes,
    middleware=[Middleware(APIKeyMiddleware)],
    lifespan=mcp_app.lifespan,  # ← required: initializes FastMCP task group
)


# ---------------------------------------------------------------------------
# Health check — always returns 200, used by load balancer / K8s probes
# ---------------------------------------------------------------------------
async def health(request: Request) -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "service": "voicebot-mcp-server",
        "tools": [
            "get_customer_info",
            "book_appointment",
            "get_order_status",
            "escalate_to_agent",
            "send_notification",
            "get_billing_info",
            "send_email",
            "send_whatsapp",
            "test_add",
        ],
        "timestamp": datetime.utcnow().isoformat(),
    })


app.routes.append(Route("/health", health))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    port    = int(os.environ.get("PORT", 8000))
    workers = int(os.environ.get("WORKERS", 1))

    logger.info("Starting MCP server on port %d with %d workers", port, workers)

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=port,
        workers=workers,
        log_level="info",
    )