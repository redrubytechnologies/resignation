import base64
import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ResCompany(models.Model):
    """Add relieving-letter specific fields to the company."""
    _inherit = 'res.company'

    relieving_letter_signature = fields.Binary(
        string='Relieving Letter Signature',
        help='Signature image printed on all Relieving & Experience Letters.'
    )
    head_office_address = fields.Char(
        string='Head Office Address',
        help='Full head office line, e.g. "747, Amara Complex, S.K.C Road, Erode - 638001. Ph: 0424-2263507"'
    )
    corporate_office_line1 = fields.Char(
        string='Corporate Office Line 1',
        help='e.g. "New No:2 Old No:7, North Crescent Road, Lakshmi Colony,"'
    )
    corporate_office_line2 = fields.Char(
        string='Corporate Office Line 2',
        help='e.g. "GN Chetty Road, T. Nagar, Chennai - 600017."'
    )
    corporate_phone = fields.Char(
        string='Corporate Phone',
        help='e.g. "044-42605110, 42618963, Mobile: +91 9791933074, 83."'
    )


class HrResignationRelievingLetter(models.Model):
    """
    Extends hr.resignation with Relieving & Experience Letter generation,
    auto-email on last working day, and mobile PDF download.
    """
    _inherit = 'hr.resignation'

    relieving_letter_sent = fields.Boolean(
        string='Relieving Letter Sent', default=False,
        help='Set to True once the letter has been emailed to the employee.'
    )
    relieving_letter_attachment_id = fields.Many2one(
        'ir.attachment', string='Relieving Letter PDF', readonly=True,
        help='Stored PDF attachment of the relieving letter.'
    )

    # ------------------------------------------------------------------ #
    #  PDF generation helper                                               #
    # ------------------------------------------------------------------ #

    def _generate_relieving_letter_pdf(self):
        """
        Renders the QWeb report to PDF and stores it as an ir.attachment.
        Returns the attachment record.
        """
        self.ensure_one()
        report = self.env.ref(
            'ent_hr_resignation.action_report_relieving_letter'
        )
        pdf_content, _ = report._render_qweb_pdf(report.report_name, res_ids=[self.id])

        file_name = 'Relieving_Letter_%s.pdf' % self.employee_id.name.replace(' ', '_')

        # Remove previous attachment if any
        if self.relieving_letter_attachment_id:
            self.relieving_letter_attachment_id.unlink()

        attachment = self.env['ir.attachment'].sudo().create({
            'name': file_name,
            'type': 'binary',
            'datas': base64.b64encode(pdf_content),
            'res_model': self._name,
            'res_id': self.id,
            'mimetype': 'application/pdf',
        })
        self.relieving_letter_attachment_id = attachment
        return attachment

    # ------------------------------------------------------------------ #
    #  Web — Print / Download button                                       #
    # ------------------------------------------------------------------ #

    def action_print_relieving_letter(self):
        """Opens the PDF preview in the browser (web button action)."""
        self.ensure_one()
        if self.state not in ('approved', 'archived'):
            raise UserError(
                'The relieving letter can only be printed for approved resignations.'
            )
        return self.env.ref(
            'ent_hr_resignation.action_report_relieving_letter'
        ).report_action(self)

    # ------------------------------------------------------------------ #
    #  Email send (manual + cron)                                          #
    # ------------------------------------------------------------------ #

    def action_send_relieving_letter(self):
        """
        Generates the PDF and emails it to the employee.
        Can be triggered manually from the web form or by the daily cron.
        """
        self.ensure_one()
        if self.state not in ('approved', 'archived'):
            raise UserError(
                'The relieving letter can only be sent for approved resignations.'
            )

        email_to = self.employee_id.work_email
        if not email_to:
            raise UserError(
                'No work email configured for employee %s.' % self.employee_id.name
            )

        attachment = self._generate_relieving_letter_pdf()

        approved_date = (
            self.approved_revealing_date.strftime('%d %B %Y')
            if self.approved_revealing_date else 'your last working day'
        )

        body_html = """
<p>Dear <strong>%s</strong>,</p>
<br/>
<p>
    Please find attached your <strong>Relieving and Experience Letter</strong>
    effective <strong>%s</strong>.
</p>
<br/>
<p>
    We thank you for your valuable contributions to <strong>%s</strong>
    and wish you all the very best in your future endeavors.
</p>
<br/>
<p>Warm Regards,<br/><strong>HR Team</strong><br/>%s</p>
        """ % (
            self.employee_id.name,
            approved_date,
            self.employee_id.company_id.name,
            self.employee_id.company_id.name,
        )

        mail = self.env['mail.mail'].sudo().create({
            'subject': 'Relieving and Experience Letter – %s' % self.employee_id.name,
            'body_html': body_html,
            'email_to': email_to,
            'email_cc': 'hrsupport@zigmaindia.com',
            'email_from': self.employee_id.company_id.email or 'hrms@Zigmaindia.com',
            'attachment_ids': [(4, attachment.id)],
            'auto_delete': False,
        })
        mail.send()

        self.relieving_letter_sent = True
        _logger.info(
            'Relieving letter sent to %s (%s)', self.employee_id.name, email_to
        )

    # ------------------------------------------------------------------ #
    #  Cron — auto-send on last working day                               #
    # ------------------------------------------------------------------ #

    @api.model
    def _cron_send_relieving_letter(self):
        """
        Called daily by the scheduled action.
        Finds all approved resignations whose last working day is TODAY
        and whose letter has not been sent yet, then emails the letter.
        Also sends a consolidated reminder to hrsupport@zigmaindia.com.
        """
        today = fields.Date.today()
        resignations = self.search([
            ('state', '=', 'approved'),
            ('approved_revealing_date', '=', today),
            ('relieving_letter_sent', '=', False),
        ])
        _logger.info(
            'Relieving letter cron: found %d resignation(s) due today.', len(resignations)
        )
        for resignation in resignations:
            try:
                resignation.action_send_relieving_letter()
            except Exception as e:
                _logger.error(
                    'Failed to send relieving letter for %s: %s',
                    resignation.employee_id.name, str(e)
                )

        # Send HR support a consolidated reminder for all last-day employees today
        try:
            self._notify_hr_support_last_day_today()
        except Exception as e:
            _logger.error('Failed to send HR support last-day reminder: %s', str(e))

    @api.model
    def _notify_hr_support_last_day_today(self):
        """
        Sends a consolidated reminder to hrsupport@zigmaindia.com listing
        all employees whose approved last working day is today.
        """
        today = fields.Date.today()
        resignations = self.search([
            ('state', '=', 'approved'),
            ('approved_revealing_date', '=', today),
        ])

        if not resignations:
            _logger.info('HR support last-day reminder: no employees with last day today.')
            return

        rows = ''
        for idx, rec in enumerate(resignations, start=1):
            rows += """
    <tr>
        <td style="padding:6px 12px; border:1px solid #ddd; text-align:center;">%d</td>
        <td style="padding:6px 12px; border:1px solid #ddd;">%s</td>
        <td style="padding:6px 12px; border:1px solid #ddd;">%s</td>
        <td style="padding:6px 12px; border:1px solid #ddd;">%s</td>
        <td style="padding:6px 12px; border:1px solid #ddd;">%s</td>
        <td style="padding:6px 12px; border:1px solid #ddd;">%s</td>
    </tr>""" % (
                idx,
                rec.employee_id.name,
                rec.employee_id.emp_id or 'N/A',
                rec.department_id.name if rec.department_id else 'N/A',
                rec.parent_id.name if rec.parent_id else 'N/A',
                rec.employee_id.work_email or 'N/A',
            )

        formatted_today = today.strftime('%d %B %Y')
        body_html = """
<p>Dear HR Support Team,</p>
<br/>
<p>
    This is a reminder that the following employee(s) have their
    <strong>last working day today (%s)</strong>.
    Please ensure all exit formalities, handover processes, and
    Full &amp; Final settlement steps are completed.
</p>
<br/>
<table style="border-collapse:collapse; font-family:Arial, sans-serif; font-size:14px; width:100%%;">
    <thead>
        <tr style="background:#4a4a4a; color:#ffffff;">
            <th style="padding:8px 12px; border:1px solid #ddd;">#</th>
            <th style="padding:8px 12px; border:1px solid #ddd;">Employee Name</th>
            <th style="padding:8px 12px; border:1px solid #ddd;">Employee Code</th>
            <th style="padding:8px 12px; border:1px solid #ddd;">Department</th>
            <th style="padding:8px 12px; border:1px solid #ddd;">Reporting Officer</th>
            <th style="padding:8px 12px; border:1px solid #ddd;">Work Email</th>
        </tr>
    </thead>
    <tbody>%s
    </tbody>
</table>
<br/>
<p>Please take the necessary action at the earliest.</p>
<br/>
<p>Regards,<br/><strong>HRMS System</strong></p>
        """ % (formatted_today, rows)

        self.env['mail.mail'].sudo().create({
            'subject': '[Last Day Reminder] %d Employee(s) Exiting Today – %s' % (len(resignations), formatted_today),
            'body_html': body_html,
            'email_to': 'hrsupport@zigmaindia.com',
            'email_from': 'hrms@Zigmaindia.com',
            'auto_delete': True,
        }).send()

        _logger.info(
            'HR support last-day reminder sent: %d employee(s) exiting on %s.',
            len(resignations), formatted_today
        )
