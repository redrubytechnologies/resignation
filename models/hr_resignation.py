
from datetime import datetime, timedelta
from odoo import models, fields, api, _
from odoo.exceptions import ValidationError

date_format = "%Y-%m-%d"
RESIGNATION_TYPE = [('resigned', 'Normal Resignation'),
                    ('fired', 'Fired by the company')]


class HrResignation(models.Model):
    _name = 'hr.resignation'
    _inherit = 'mail.thread'
    _rec_name = 'employee_id'

    name = fields.Char(string='Order Reference', required=True, copy=False, readonly=True, index=True,
                       default=lambda self: _('New'))
    employee_id = fields.Many2one('hr.employee', string="Employee", default=lambda self: self.env.user.employee_id.id,
                                  help='Name of the employee for whom the request is creating')
    department_id = fields.Many2one('hr.department', string="Department", related='employee_id.department_id',
                                    help='Department of the employee')
    parent_id=fields.Many2one(related="employee_id.parent_id")

    applied_date = fields.Date(
        string="Applied Date", readonly=True,
        help='Date on which the employee applied the resignation.')
    resign_confirm_date = fields.Date(string="Confirmed Date",
                                      help='Date on which the request is confirmed by the employee.',
                                      track_visibility="always")
    reporting_officer_confirm_date = fields.Date(
        string="Reporting Officer Confirm Date", readonly=True,
        help='Date on which the reporting officer confirmed the resignation.')
    hr_approved_date = fields.Date(
        string="HR Approved Date", readonly=True,
        help='Date on which HR approved the resignation.')
    approved_revealing_date = fields.Date(string="Approved Last Day Of Employee",
                                          help='Date on which the request is confirmed by the manager.',
                                          track_visibility="always")
    joined_date = fields.Date(string="Join Date", store=True,
                              help='Joining date of the employee.i.e Start date of the first contract')

    expected_revealing_date = fields.Date(string="Last Day of Employee", required=True,
                                          help='Employee requested date on which he is revealing from the company.')
    reason = fields.Many2one('hr.resignation.reason',string="Reason", help='Specify reason for leaving the company')
    employee_reason = fields.Text(string="Employee Reason", help='Reason provided by the employee for leaving the company')

    notice_period = fields.Char(
        string="Notice Period",
        compute='_compute_notice_period',
        store=True,
        readonly=False,
        default='30',
    )

    noc_attachment_ids = fields.Many2many(
        'ir.attachment',
        'hr_resignation_noc_rel',
        'resignation_id', 'attachment_id',
        string='NOC Attachments',
        help='Upload the No Objection Certificate(s) here.'
    )
    state = fields.Selection(
        [('draft', 'Draft'), ('confirm', 'Confirm'), ('approved', 'Approved'), ('cancel', 'Rejected'),
         ('archived', 'Archived')],
        string='Status', default='draft', track_visibility="always")

    resignation_type = fields.Selection(selection=RESIGNATION_TYPE, required=True,
                                        help="Select the type of resignation: normal "
                                             "resignation or fired by the company")
    read_only = fields.Boolean(string="check field")
    employee_contract = fields.Char(String="Contract",store=True)
    employee_name = fields.Char(string='Employee Name', related='employee_id.name', readonly=True)
    employee_domain = fields.Char(string="Employee Domain", compute="_compute_employee_domain", store=False)
    reporting_officer_remarks = fields.Text(string="Reporting Officer Remarks", track_visibility="always")
    hr_remarks = fields.Text(string="HR Remarks", track_visibility="always")
    @api.onchange('employee_id')
    @api.depends('employee_id')
    def _compute_read_only(self):
        """ Use this function to check whether the user has the permission to change the employee"""
        res_user = self.env['res.users'].search([('id', '=', self._uid)])
        if res_user.has_group('hr.group_hr_user'):
            self.read_only = True
        else:
            self.read_only = False

    @api.onchange('employee_id')
    def set_join_date(self):
     for rec in self:
        if rec.employee_id and not rec.joined_date:
            rec.joined_date = rec.employee_id.joining_date

    joined_date = fields.Date(string="Join Date", store=True,
                          help='Joining date of the employee.i.e Start date of the first contract', readonly=True)
 



    @api.model
    def create(self, vals):
        res = super(HrResignation, self).create(vals)
        # Assign user_id based on the employee's user_id
        if res.employee_id.user_id:
            res.user_id = res.employee_id.user_id
        return res

    @api.constrains('employee_id')
    def check_employee(self):
        # Checking whether the user is creating a leave request for his/her own
        for rec in self:
            if not self.env.user.has_group('hr.group_hr_user'):
                if rec.employee_id.user_id.id and rec.employee_id.user_id.id != self.env.uid:
                    raise ValidationError(_('You cannot create a request for other employees'))

    @api.onchange('employee_id')
    @api.depends('employee_id')
    def check_request_existence(self):
        # Check whether any resignation request already exists
        for rec in self:
            if rec.employee_id:
                resignation_request = self.env['hr.resignation'].search([('employee_id', '=', rec.employee_id.id),
                                                                         ('state', 'in', ['confirm', 'approved'])])
                if resignation_request:
                    raise ValidationError(_('There is a resignation request in the confirmed or'
                                            ' approved state for this employee'))
                if rec.employee_id:
                    no_of_contract = self.env['hr.contract'].search([('employee_id', '=', self.employee_id.id)])
                    for contracts in no_of_contract:
                        if contracts.state == 'open':
                            rec.employee_contract = contracts.name
                            rec.notice_period = contracts.notice_days

    @api.constrains('joined_date')
    def _check_dates(self):
        # validating the entered dates
        for rec in self:
            resignation_request = self.env['hr.resignation'].search([('employee_id', '=', rec.employee_id.id),
                                                                     ('state', 'in', ['confirm', 'approved'])])
            if resignation_request:
                raise ValidationError(_('There is a resignation request in the confirmed or'
                                        ' approved state for this employee'))

    def confirm_resignation(self):
        for rec in self:
            if not rec.joined_date:
                rec.joined_date = rec.employee_id.joining_date

            if rec.employee_id.joining_date:
                if rec.employee_id.joining_date >= rec.expected_revealing_date:
                    raise ValidationError(_('The last date of the Employee must be anterior to the Joining date'))

            rec.state = 'confirm'
            rec.resign_confirm_date = fields.Date.today()
            rec.reporting_officer_confirm_date = fields.Date.today()
            rec._send_resignation_ack_email()

    def cancel_resignation(self):
        for rec in self:
            rec.state = 'cancel'

    def reject_resignation(self):
        for rec in self:
            rec.state = 'cancel'

    def reset_to_draft(self):
        for rec in self:
            rec.state = 'draft'
            rec.employee_id.active = True
            rec.employee_id.resigned = False
            rec.employee_id.fired = False
            rec.employee_id.notice_period_approved = False

    def action_reporting_officer_confirm(self):
        """Reporting officer confirms the resignation (web + mobile)."""
        for rec in self:
            rec.reporting_officer_confirm_date = fields.Date.today()

    def approve_resignation(self):
        for rec in self:
            if rec.expected_revealing_date and rec.resign_confirm_date:
                if rec.resignation_type == 'resigned':
                    rec.employee_id.notice_period_approved = True
                    # Keep whatever HR entered; only fall back to employee's requested date if blank
                    if not rec.approved_revealing_date:
                        rec.approved_revealing_date = rec.expected_revealing_date
                    rec.hr_approved_date = fields.Date.today()
                    # notice_period recomputed automatically via @api.depends
                    rec.state = 'approved'
                else:
                    rec.employee_id.fired = True
                    rec.hr_approved_date = fields.Date.today()
                    rec.state = 'approved'
                rec._send_resignation_approval_email()
            else:
                raise ValidationError(_('Please enter valid dates.'))

    def _send_resignation_approval_email(self):
        """Send approval notification email to the employee when HR approves the resignation."""
        for rec in self:
            email_to = rec.employee_id.work_email
            if not email_to:
                continue

            last_working_day = (
                rec.approved_revealing_date.strftime('%d %B %Y')
                if rec.approved_revealing_date else 'To be confirmed'
            )
            company_name = rec.employee_id.company_id.name or 'Zigma Technologies India (P) Limited'

            if rec.resignation_type == 'resigned':
                subject_line = 'Resignation Approval – %s' % rec.employee_id.name
                body_html = """
<p>Dear <strong>%s</strong>,</p>
<br/>
<p>
    We would like to inform you that your resignation has been
    <strong>reviewed and approved</strong> by <strong>%s</strong>.
</p>
<br/>
<p>
    Your <strong>last working day</strong> has been confirmed as
    <strong>%s</strong>.
</p>
<br/>
<p>
    On your last working day, you will be required to complete the
    <strong>Exit Process</strong>. This can be done either:
</p>
<ul>
    <li><strong>Via the Mobile App</strong> – complete the exit interview digitally, or</li>
    <li><strong>In Person (Face-to-Face)</strong> – attend a scheduled exit interview with the HR team.</li>
</ul>
<br/>
<p>
    Please ensure all pending tasks, handovers, and formalities are completed before
    your last working day. Your <strong>Full &amp; Final Settlement</strong> and
    <strong>Experience Letter</strong> will be processed post completion of the exit formalities.
</p>
<br/>
<p>We sincerely thank you for your contributions to <strong>%s</strong> and wish you the very best in your future endeavors.</p>
<br/>
<p>Warm Regards,<br/><strong>HR Team</strong><br/><strong>%s</strong></p>
                """ % (
                    rec.employee_id.name,
                    company_name,
                    last_working_day,
                    company_name,
                    company_name,
                )
            else:
                subject_line = 'Employment Termination Notice – %s' % rec.employee_id.name
                body_html = """
<p>Dear <strong>%s</strong>,</p>
<br/>
<p>
    This is to formally inform you that your employment with
    <strong>%s</strong> has been terminated effective
    <strong>%s</strong>.
</p>
<br/>
<p>
    You are required to complete the <strong>Exit Process</strong> on your last working day
    either via the <strong>Mobile App</strong> or through a <strong>Face-to-Face</strong>
    session with the HR team.
</p>
<br/>
<p>
    Please ensure all company assets are returned and pending formalities are completed
    before or on your last working day.
</p>
<br/>
<p>Regards,<br/><strong>HR Team</strong><br/><strong>%s</strong></p>
                """ % (
                    rec.employee_id.name,
                    company_name,
                    last_working_day,
                    company_name,
                )

            self.env['mail.mail'].sudo().create({
                'subject': subject_line,
                'body_html': body_html,
                'email_to': email_to,
                'email_cc': 'hrsupport@zigmaindia.com',
                'email_from': rec.employee_id.company_id.email or 'hrms@Zigmaindia.com',
                'auto_delete': True,
            }).send()

    def update_employee_status(self):
        resignation = self.env['hr.resignation'].search([('state', '=', 'approved')])
        for rec in resignation:
            if rec.expected_revealing_date <= fields.Date.today() and rec.employee_id.active:
                rec.employee_id.active = False
                # Changing fields in the employee table with respect to resignation
                rec.employee_id.resign_date = rec.expected_revealing_date
                if rec.resignation_type == 'resigned':
                    rec.employee_id.resigned = True
                else:
                    rec.employee_id.fired = True
                # Removing and deactivating the user
                if rec.employee_id.user_id:
                    rec.employee_id.user_id.active = False
                    rec.employee_id.user_id = None

    @api.depends('approved_revealing_date', 'hr_approved_date')
    def _compute_notice_period(self):
        """Recomputes notice period only when HR sets an approved last day.
        New records get the field's default ('30'). Mobile/email values are never overridden here.
        """
        for rec in self:
            if rec.approved_revealing_date:
                ref_date = rec.hr_approved_date or fields.Date.today()
                delta = (rec.approved_revealing_date - ref_date).days
                rec.notice_period = str(max(delta, 0))

    @api.onchange('notice_period')
    def _onchange_notice_period_update_date(self):
        """User edits notice period → recompute last day (UI only, not programmatic).
        Draft/Confirm: updates expected_revealing_date (employee side).
        Approved: updates approved_revealing_date (HR side).
        """
        for rec in self:
            if rec.notice_period:
                try:
                    days = int(rec.notice_period)
                    if days < 0:
                        raise ValidationError(_('Notice period must be a positive number.'))
                    if rec.state in ('draft', 'confirm'):
                        rec.expected_revealing_date = fields.Date.today() + timedelta(days=days)
                    else:
                        ref_date = rec.hr_approved_date or fields.Date.today()
                        rec.approved_revealing_date = ref_date + timedelta(days=days)
                except ValueError:
                    pass

    @api.onchange('expected_revealing_date')
    def _onchange_expected_date_update_notice_period(self):
        """User edits expected last day → recompute notice period (UI only, not programmatic)."""
        for rec in self:
            if rec.expected_revealing_date and rec.state in ('draft', 'confirm'):
                delta = (rec.expected_revealing_date - fields.Date.today()).days
                rec.notice_period = str(max(delta, 0))

    def deactivate_login_access(self):
        for resignation in self:
            if resignation.state == 'approved' and resignation.employee_id.user_id:
                resignation.employee_id.user_id.write({'active': False})
                resignation.employee_id.write({'active': False})
                resignation.deactivate_button_clicked = True
        return True

    deactivate_button_clicked = fields.Boolean(string="Deactivate Button Clicked")

    @api.model
    def create(self, vals):
        if vals.get('employee_id'):
            employee = self.env['hr.employee'].browse(vals['employee_id'])
            vals['name'] = employee.name or _('New')
        else:
            vals['name'] = _('New')
        if not vals.get('applied_date'):
            vals['applied_date'] = fields.Date.today()
        return super(HrResignation, self).create(vals)

    def archive_resignation(self):
        for resignation in self:
            if resignation.state == 'approved':
                if resignation.employee_id.user_id:
                    resignation.employee_id.user_id.write({'active': False})
                resignation.employee_id.write({'active': False})
                resignation.employee_id.resigned = True if resignation.resignation_type == 'resigned' else False
                resignation.employee_id.fired = True if resignation.resignation_type == 'fired' else False
                resignation.deactivate_button_clicked = True
                resignation.state = 'archived'  # Change state to 'archived'
        return True

    def activate_resignation(self):
        for resignation in self:
            if resignation.state == 'archived':
                if resignation.employee_id.user_id:
                    resignation.employee_id.user_id.write({'active': True})  # Activate user
                resignation.employee_id.write({'active': True})  # Activate employee
                resignation.deactivate_button_clicked = False
                resignation.state = 'approved'  # Change state back to 'approved'
        return True


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    resign_date = fields.Date(string="Resign Date")
    resigned = fields.Boolean(string="Resigned", default=False, store=True, help="If checked then employee has resigned")
    fired = fields.Boolean(string="Fired", default=False, help="If checked then employee has been fired")
    notice_period_approved = fields.Boolean(string="Notice Period Approved", default=False,
                                            help="Indicates if the notice period is approved")
    ribbon_color = fields.Selection([('success', 'Active'), ('danger', 'Resigned'), ('warning', 'Notice Period')],
                                    string="Ribbon Color", default='success')
    state = fields.Selection(
        [('draft', 'Draft'), ('confirm', 'Confirm'), ('approved', 'Approved'), ('cancel', 'Rejected'),
         ('archived', 'Archived')],
        string='Status', default='draft', track_visibility="always")
    employee_id = fields.Many2one('hr.employee', string="Employee",
                                  default=lambda self: self.env.user.employee_id.id,
                                  help='Name of the employee for whom the request is creating')

    reporting_officer_id = fields.Many2one(
        'hr.employee',
        string='Reporting Officer',
        domain="[('job_id.name', '=', 'HR')]",
        groups="custom_hr_groups.group_hr"
    )
   

    ribbon_color = fields.Char(string="Notice Period Remaining", compute="_compute_ribbon_color", store=True)

    @api.depends('resign_date', 'notice_period_approved')
    def _compute_ribbon_color(self):
        for employee in self:
            if employee.resign_date and employee.notice_period_approved:
                last_resignation = self.env['hr.resignation'].search([('employee_id', '=', employee.id), ('state', '=', 'approved')], order='approved_revealing_date desc', limit=1)
                if last_resignation:
                    last_approval_date = last_resignation.approved_revealing_date
                    current_date = fields.Date.today()
                    remaining_days = (last_approval_date - current_date).days
                    if remaining_days <= 0:
                        employee.ribbon_color = 'Resigned'  # Employee has resigned
                    else:
                        employee.ribbon_color = f'Notice Period {remaining_days}'  # Notice period count
                else:
                    employee.ribbon_color = 'No Active Resignation'  # No active resignation
            else:
                employee.ribbon_color = 'No Active Resignation'  # No active resignation or notice period approved


    def action_resignation_confirmation(self):
        for employee in self:
            employee.resigned = True
            employee.ribbon_color = 'danger'
            # Other actions you want to perform upon resignation confirmation
        return True

    def action_UnResign(self):
        self.state = 'draft'
        self.active = True
        self.user_id.active = True
        self.resigned = False
        self.fired = False
        self.notice_period_approved = False
        resignation = self.env['hr.resignation'].search([('employee_id', '=', self.id)])
        resignation.reset_to_draft()

    def delete_employee(self):
        self.unlink()
        self.user_id.unlink()

    
class HRResignReason(models.Model):
    _name = 'hr.resignation.reason'

    name=fields.Char(string="Reason")