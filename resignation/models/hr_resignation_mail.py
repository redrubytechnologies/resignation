import re
import logging
from datetime import timedelta

from odoo import models, fields, api, _
from odoo.tools import html2plaintext

_logger = logging.getLogger(__name__)

# Keywords that indicate a resignation email
RESIGNATION_KEYWORDS = [
    'resignation', 'resign', 'termination', 'terminate',
    'emergency', 'last working day', 'notice period',
    'relieving', 'quit', 'leaving', 'notice',
]

# Matches employee codes like ZT000156, ZT 000156, zt00156
EMPLOYEE_ID_REGEX = re.compile(r'\b(ZT\s*\d{4,6})\b', re.IGNORECASE)


class HrResignationInbox(models.Model):
    """
    Receives and processes resignation emails sent to the configured mail alias
    (e.g. resignation@company.com).

    Each incoming email creates one inbox record. The model:
      1. Checks the subject/body for resignation keywords.
      2. Resolves the employee from the employee ID in the subject or
         the sender's work email address.
      3. Auto-creates an hr.resignation record for the matched employee.
      4. Sends a formal acknowledgement email to the employee.
    """
    _name = 'hr.resignation.inbox'
    _description = 'Resignation Email Inbox'
    _inherit = ['mail.thread']
    _rec_name = 'subject'
    _order = 'received_date desc'

    subject = fields.Char(string='Email Subject')
    email_from = fields.Char(string='Sender Email')
    received_date = fields.Datetime(string='Received On', default=fields.Datetime.now)
    raw_body = fields.Html(string='Email Body')
    employee_id = fields.Many2one('hr.employee', string='Matched Employee')
    resignation_id = fields.Many2one('hr.resignation', string='Created Resignation')
    state = fields.Selection([
        ('processed', 'Processed'),
        ('unresolved', 'Unresolved'),
        ('duplicate', 'Duplicate'),
    ], string='Status', default='unresolved')
    notes = fields.Text(string='Processing Notes')

    # ------------------------------------------------------------------ #
    #  Incoming email handler                                              #
    # ------------------------------------------------------------------ #

    @api.model
    def message_new(self, msg_dict, custom_values=None):
        """Called by Odoo for each email arriving at the mail alias."""
        subject = msg_dict.get('subject', '') or ''
        body = msg_dict.get('body', '') or ''
        email_from = msg_dict.get('email_from', '') or ''
        body_plain = html2plaintext(body)

        search_text = (subject + ' ' + body_plain).lower()
        has_keyword = any(kw in search_text for kw in RESIGNATION_KEYWORDS)

        vals = {
            'subject': subject,
            'email_from': email_from,
            'raw_body': body,
        }
        if custom_values:
            vals.update(custom_values)

        # ── Not a resignation email — skip entirely, create no record ── #
        if not has_keyword:
            _logger.info(
                'Resignation inbox: skipping email "%s" from <%s> — no resignation keywords found.',
                subject, email_from
            )
            return self.env[self._name]

        # ── Try to identify the employee ─────────────────────────────── #
        employee = self._resolve_employee(subject, body_plain, email_from)

        if not employee:
            vals.update({
                'state': 'unresolved',
                'notes': (
                    'Could not match an employee. '
                    'No valid employee ID found in subject/body and sender email did not match any work email.'
                ),
            })
            inbox = super().message_new(msg_dict, vals)
            inbox._notify_hr_unresolved()
            return inbox

        # ── Block duplicate resignation ──────────────────────────────── #
        existing = self.env['hr.resignation'].sudo().search([
            ('employee_id', '=', employee.id),
            ('state', 'in', ['draft', 'confirm', 'approved']),
        ], limit=1)

        if existing:
            vals.update({
                'state': 'duplicate',
                'employee_id': employee.id,
                'resignation_id': existing.id,
                'notes': 'Resignation already active (Ref: %s). Duplicate email ignored.' % existing.name,
            })
            return super().message_new(msg_dict, vals)

        # ── Create the resignation record ────────────────────────────── #
        resignation = self._create_resignation_from_email(employee, subject, body_plain)

        vals.update({
            'state': 'processed',
            'employee_id': employee.id,
            'resignation_id': resignation.id,
            'notes': 'Resignation auto-created (Ref: %s).' % resignation.name,
        })

        inbox = super().message_new(msg_dict, vals)

        # Send formal acknowledgement to the employee
        resignation._send_resignation_ack_email()

        return inbox

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _resolve_employee(self, subject, body_plain, email_from):
        """
        Try to find the employee using three strategies in order:
          1. Employee code (ZT-format) in the email subject
          2. Employee code in the email body
          3. Sender work email address
        """
        for text in (subject, body_plain):
            match = EMPLOYEE_ID_REGEX.search(text)
            if match:
                raw_id = re.sub(r'\s+', '', match.group(1)).upper()
                employee = self.env['hr.employee'].sudo().search(
                    [('emp_id', '=ilike', raw_id)], limit=1
                )
                if employee:
                    return employee

        # Fallback: match by work email
        if email_from:
            email_match = re.search(r'[\w.\-+]+@[\w.\-]+', email_from)
            if email_match:
                sender_email = email_match.group(0).lower()
                employee = self.env['hr.employee'].sudo().search(
                    [('work_email', '=ilike', sender_email)], limit=1
                )
                if employee:
                    return employee

        return None

    def _create_resignation_from_email(self, employee, subject, body_plain=''):
        """Create and return a draft hr.resignation for the given employee."""
        notice_period = '30'
        employee_contract = None
        contract = self.env['hr.contract'].sudo().search(
            [('employee_id', '=', employee.id), ('state', '=', 'open')], limit=1
        )
        if contract:
            notice_period = str(int(contract.notice_days)) if contract.notice_days else '30'
            employee_contract = contract.name

        tentative_last_day = fields.Date.today() + timedelta(days=int(notice_period))

        return self.env['hr.resignation'].sudo().create({
            'employee_id': employee.id,
            'resignation_type': 'resigned',
            'expected_revealing_date': tentative_last_day,
            'notice_period': notice_period,
            'employee_contract': employee_contract,
            'joined_date': employee.joining_date,
            'applied_date': fields.Date.today(),
            'employee_reason': body_plain.strip() if body_plain else '',
        })

    def _notify_hr_unresolved(self):
        """Email all HR users when an incoming mail cannot be matched to an employee."""
        hr_group = self.env.ref('hr.group_hr_user', raise_if_not_found=False)
        if not hr_group:
            return
        hr_emails = [u.email for u in hr_group.users if u.email]
        if not hr_emails:
            return

        self.env['mail.mail'].sudo().create({
            'subject': '[Action Required] Unresolved Resignation Email',
            'body_html': """
                <p>Dear HR Team,</p>
                <p>A resignation email was received but could not be matched to any employee.</p>
                <table style="border-collapse:collapse;">
                    <tr><td style="padding:4px 8px;"><strong>Subject</strong></td><td style="padding:4px 8px;">%s</td></tr>
                    <tr><td style="padding:4px 8px;"><strong>From</strong></td><td style="padding:4px 8px;">%s</td></tr>
                    <tr><td style="padding:4px 8px;"><strong>Received</strong></td><td style="padding:4px 8px;">%s</td></tr>
                </table>
                <br/>
                <p>Please review it manually in <strong>Resignation &gt; Resignation Email Inbox</strong>.</p>
                <p>Regards,<br/><strong>HRMS System</strong></p>
            """ % (self.subject, self.email_from, self.received_date),
            'email_to': ','.join(hr_emails),
            'email_from': 'hrms@Zigmaindia.com',
        }).send()


class HrResignationAck(models.Model):
    """Adds the acknowledgement email method to hr.resignation."""
    _inherit = 'hr.resignation'

    def _send_resignation_ack_email(self):
        """
        Send a formal resignation acknowledgement email to the employee.
        Called automatically when a resignation is auto-created from an email.
        """
        for rec in self:
            employee = rec.employee_id
            email_to = employee.work_email
            if not email_to:
                _logger.warning(
                    'Resignation ACK email skipped: no work_email for employee %s', employee.name
                )
                continue

            notice_days = int(rec.notice_period or '0')
            tentative_last_day = rec.expected_revealing_date
            formatted_last_day = (
                tentative_last_day.strftime('%d %B %Y') if tentative_last_day else 'To be confirmed'
            )

            body_html = """
<p>Dear <strong>%s</strong>,</p>
<br/>
<p>We acknowledge the receipt of your resignation mail dated <strong>%s</strong>.</p>
<br/>
<p>Your resignation has been <strong>formally accepted</strong>.</p>
<br/>
<p>
    Based on your notice period of <strong>%s day(s)</strong>, your
    <strong>tentative last working day is %s</strong>.
    This date is subject to change based on the submission and approval of your
    <strong>No Objection Certificate (NOC)</strong>.
</p>
<br/>
<p>
    We kindly request you to <strong>submit your NOC</strong> at the earliest so that
    your last working day can be formally confirmed.
</p>
<br/>
<p>On your last working day, post completion of your <strong>Exit Interview</strong>:</p>
<ul>
    <li>Your <strong>Full &amp; Final Settlement</strong> will be processed.</li>
    <li>Your <strong>Experience Letter</strong> will be issued.</li>
</ul>
<br/>
<p>
    Please note that the above will be subject to the satisfactory completion of all exit formalities
    as per company policy.
</p>
<br/>
<p>We wish you all the very best in your future endeavors.</p>
<br/>
<p>Warm Regards,<br/><strong>HR Team</strong></p>
            """ % (
                employee.name,
                fields.Date.today().strftime('%d %B %Y'),
                notice_days,
                formatted_last_day,
            )

            self.env['mail.mail'].sudo().create({
                'subject': 'Resignation Acknowledgement – %s' % employee.name,
                'body_html': body_html,
                'email_to': email_to,
                'email_cc': 'hrsupport@zigmaindia.com',
                'email_from': 'hrms@Zigmaindia.com',
                'auto_delete': True,
            }).send()

            _logger.info('Resignation ACK email sent to %s (%s)', employee.name, email_to)

    def _notify_hr_support_mobile_submission(self):
        """
        Send a dedicated notification to hrsupport@zigmaindia.com when an
        employee submits a resignation via the mobile app.
        """
        for rec in self:
            employee = rec.employee_id
            applied_date = rec.applied_date.strftime('%d %B %Y') if rec.applied_date else 'N/A'
            expected_last_day = (
                rec.expected_revealing_date.strftime('%d %B %Y')
                if rec.expected_revealing_date else 'N/A'
            )
            department = rec.department_id.name if rec.department_id else 'N/A'
            reporting_officer = rec.parent_id.name if rec.parent_id else 'N/A'

            body_html = """
<p>Dear HR Support Team,</p>
<br/>
<p>A new resignation has been submitted via the <strong>Mobile App</strong>. Please find the details below:</p>
<br/>
<table style="border-collapse:collapse; font-family:Arial, sans-serif; font-size:14px;">
    <tr>
        <td style="padding:6px 12px; font-weight:bold; background:#f5f5f5; border:1px solid #ddd;">Employee Name</td>
        <td style="padding:6px 12px; border:1px solid #ddd;">%s</td>
    </tr>
    <tr>
        <td style="padding:6px 12px; font-weight:bold; background:#f5f5f5; border:1px solid #ddd;">Employee Code</td>
        <td style="padding:6px 12px; border:1px solid #ddd;">%s</td>
    </tr>
    <tr>
        <td style="padding:6px 12px; font-weight:bold; background:#f5f5f5; border:1px solid #ddd;">Department</td>
        <td style="padding:6px 12px; border:1px solid #ddd;">%s</td>
    </tr>
    <tr>
        <td style="padding:6px 12px; font-weight:bold; background:#f5f5f5; border:1px solid #ddd;">Reporting Officer</td>
        <td style="padding:6px 12px; border:1px solid #ddd;">%s</td>
    </tr>
    <tr>
        <td style="padding:6px 12px; font-weight:bold; background:#f5f5f5; border:1px solid #ddd;">Applied Date</td>
        <td style="padding:6px 12px; border:1px solid #ddd;">%s</td>
    </tr>
    <tr>
        <td style="padding:6px 12px; font-weight:bold; background:#f5f5f5; border:1px solid #ddd;">Expected Last Day</td>
        <td style="padding:6px 12px; border:1px solid #ddd;">%s</td>
    </tr>
    <tr>
        <td style="padding:6px 12px; font-weight:bold; background:#f5f5f5; border:1px solid #ddd;">Notice Period</td>
        <td style="padding:6px 12px; border:1px solid #ddd;">%s day(s)</td>
    </tr>
    <tr>
        <td style="padding:6px 12px; font-weight:bold; background:#f5f5f5; border:1px solid #ddd;">Reference</td>
        <td style="padding:6px 12px; border:1px solid #ddd;">%s</td>
    </tr>
</table>
<br/>
<p>Please review and process this resignation at the earliest.</p>
<br/>
<p>Regards,<br/><strong>HRMS System</strong></p>
            """ % (
                employee.name,
                employee.emp_id or 'N/A',
                department,
                reporting_officer,
                applied_date,
                expected_last_day,
                rec.notice_period or 'N/A',
                rec.name,
            )

            self.env['mail.mail'].sudo().create({
                'subject': '[New Resignation] %s – Submitted via Mobile App' % employee.name,
                'body_html': body_html,
                'email_to': 'hrsupport@zigmaindia.com',
                'email_from': 'hrms@Zigmaindia.com',
                'auto_delete': True,
            }).send()

            _logger.info(
                'HR support notification sent for mobile resignation by %s (%s)',
                employee.name, employee.emp_id or 'N/A'
            )
