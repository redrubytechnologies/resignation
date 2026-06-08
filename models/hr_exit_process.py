from odoo import models, fields, api
from odoo.exceptions import ValidationError


class HrExitQuestion(models.Model):
    _name = 'hr.exit.question'
    _description = 'Exit Interview Question'
    _order = 'sequence, id'

    name = fields.Char(string='Question', required=True)
    answer_type = fields.Selection([
        ('yes_no', 'Yes / No'),
        ('dropdown', 'Dropdown'),
        ('text', 'Text'),
    ], string='Answer Type', required=True, default='text')
    option_ids = fields.One2many('hr.exit.question.option', 'question_id', string='Dropdown Options')
    sequence = fields.Integer(string='Sequence', default=10)
    active = fields.Boolean(default=True)

    @api.constrains('answer_type', 'option_ids')
    def _check_dropdown_options(self):
        for rec in self:
            if rec.answer_type == 'dropdown' and not rec.option_ids:
                raise ValidationError(
                    'Please add at least one option for dropdown question: "%s"' % rec.name
                )


class HrExitQuestionOption(models.Model):
    _name = 'hr.exit.question.option'
    _description = 'Exit Question Dropdown Option'
    _order = 'sequence, id'

    question_id = fields.Many2one(
        'hr.exit.question', string='Question', required=True, ondelete='cascade'
    )
    name = fields.Char(string='Option', required=True)
    sequence = fields.Integer(default=10)


class HrExitAnswer(models.Model):
    _name = 'hr.exit.answer'
    _description = 'Exit Interview Answer'

    resignation_id = fields.Many2one(
        'hr.resignation', string='Resignation', required=True, ondelete='cascade'
    )
    question_id = fields.Many2one(
        'hr.exit.question', string='Question', required=True
    )
    answer_type = fields.Selection(
        related='question_id.answer_type', store=True, readonly=True
    )
    # --- answer fields (only one is relevant depending on answer_type) ---
    answer_yes_no = fields.Selection(
        [('yes', 'Yes'), ('no', 'No')], string='Answer'
    )
    answer_dropdown_id = fields.Many2one(
        'hr.exit.question.option', string='Answer',
        domain="[('question_id', '=', question_id)]"
    )
    answer_text = fields.Text(string='Answer')


class HrResignationExitProcess(models.Model):
    """Extends hr.resignation with exit interview fields."""
    _inherit = 'hr.resignation'

    exit_process_type = fields.Selection([
        ('face_to_face', 'Face to Face'),
        ('mobile', 'Via Mobile'),
    ], string='Exit Process', default='face_to_face',
        help='Face to Face: HR fills answers in the web form.\n'
             'Via Mobile: Employee answers questions through the mobile app.')

    exit_answer_ids = fields.One2many(
        'hr.exit.answer', 'resignation_id', string='Exit Interview Answers'
    )

    exit_interview_submitted = fields.Boolean(
        string='Exit Interview Submitted', default=False,
        help='Tick once the exit interview is completed — via mobile app or face to face.'
    )
    exit_interview_submitted_date = fields.Datetime(
        string='Exit Interview Submitted On', readonly=True,
        help='Date and time when the exit interview was submitted (mobile or face to face).'
    )

    def write(self, vals):
        """Auto-stamp exit_interview_submitted_date when the submitted flag is turned on."""
        if vals.get('exit_interview_submitted') and not vals.get('exit_interview_submitted_date'):
            # Only stamp for records that haven't been submitted yet
            unsubmitted = self.filtered(lambda r: not r.exit_interview_submitted)
            if unsubmitted:
                super(HrResignationExitProcess, unsubmitted).write(
                    dict(vals, exit_interview_submitted_date=fields.Datetime.now())
                )
                already = self - unsubmitted
                if already:
                    super(HrResignationExitProcess, already).write(vals)
                return True
        return super().write(vals)

    def action_generate_exit_questions(self):
        """Populate exit_answer_ids from all active exit questions."""
        for rec in self:
            existing_question_ids = rec.exit_answer_ids.mapped('question_id').ids
            questions = self.env['hr.exit.question'].search([
                ('active', '=', True),
                ('id', 'not in', existing_question_ids),
            ])
            for question in questions:
                self.env['hr.exit.answer'].create({
                    'resignation_id': rec.id,
                    'question_id': question.id,
                })
