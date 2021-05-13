# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models, _
from odoo.exceptions import RedirectWarning, UserError, ValidationError, AccessError
from odoo.tools import float_compare, date_utils, email_split, email_re
from odoo.tools.misc import formatLang, format_date, get_lang

from datetime import date, timedelta
from collections import defaultdict
from itertools import zip_longest
from hashlib import sha256
from json import dumps

import ast
import json
import re
import warnings


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"
    
    witholding_tax_ids = fields.Many2many(
        comodel_name='account.tax',
        relation='l10n_ec_witholding_account_move_line',
        string="Witholdings",
        context={'active_test': False},
        check_company=True,
        help="Witholding taxes that apply on the base amount")

    def _get_computed_wth(self):
        self.ensure_one()

        if self.move_id.is_sale_document(include_receipts=True):
            # Out invoice.
            if self.product_id.witholding_tax_ids:
                tax_ids = self.product_id.witholding_tax_ids.filtered(lambda tax: tax.company_id == self.move_id.company_id)
                return tax_ids
        return None

    @api.onchange('product_id')
    def _onchange_product_id(self):
        for line in self:
            if not line.product_id or line.display_type in ('line_section', 'line_note'):
                continue

            line.name = line._get_computed_name()
            line.account_id = line._get_computed_account()
            line.tax_ids = line._get_computed_taxes()
            line.witholding_tax_ids = line._get_computed_wth()
            line.product_uom_id = line._get_computed_uom()
            line.price_unit = line._get_computed_price_unit()

            # price_unit and taxes may need to be adapted following Fiscal Position
            line._set_price_and_tax_after_fpos()

            # Convert the unit price to the invoice's currency.
            company = line.move_id.company_id
            line.price_unit = company.currency_id._convert(line.price_unit, line.move_id.currency_id, company, line.move_id.date, round=False)

    def _set_price_and_tax_after_fpos(self):
        self.ensure_one()
        super(AccountMoveLine,self)._set_price_and_tax_after_fpos()
        if self.tax_ids and self.move_id.fiscal_position_id and self.move_id.fiscal_position_id.tax_ids:
            self.witholding_tax_ids = self.move_id.fiscal_position_id.map_tax(
                self.witholding_tax_ids._origin,
                partner=self.move_id.partner_id)

class AccountMove(models.Model):
    _inherit = "account.move"

    amount_by_group_wth = fields.Binary(string="Witholding amount by group",
        compute='_compute_invoice_witholding_by_group',
        help='Edit Tax amounts if you encounter rounding issues.')

    @api.depends('line_ids.price_subtotal', 'line_ids.tax_base_amount', 'line_ids.witholding_tax_ids', 'partner_id', 'currency_id')
    def _compute_invoice_witholding_by_group(self):
        ''' Helper to get the taxes grouped according their account.tax.group.
        This method is only used when printing the invoice.
        '''
        for move in self:
            lang_env = move.with_context(lang=move.partner_id.lang).env
            res = {}
            # There are as many tax line as there are repartition lines
            done_taxes = set()

            # At this point we only want to keep the taxes with a zero amount since they do not
            # generate a tax line.
            for line in move.line_ids:
                for tax in line.witholding_tax_ids.flatten_taxes_hierarchy():
                    #if tax.tax_group_id not in res or tax.tax_group_id in zero_taxes:
                    res.setdefault(tax.tax_group_id, {'base': 0.0, 'amount': 0.0})
                    #res[tax.tax_group_id]['base'] += [t['base'] for t in tax.compute_all(line.price_unit,self.currency_id, line.quantity)['taxes']]
                    res[tax.tax_group_id]['amount'] += sum([t['amount'] for t in tax.compute_all(line.price_unit,self.currency_id, line.quantity)['taxes']])
                    
            res = sorted(res.items(), key=lambda l: l[0].sequence)
            move.amount_by_group_wth = [(
                group.name, amounts['amount'],
                amounts['base'],
                formatLang(lang_env, amounts['amount'], currency_obj=move.currency_id),
                formatLang(lang_env, amounts['base'], currency_obj=move.currency_id),
                len(res),
                group.id
            ) for group, amounts in res]

    
    def _get_formatted_sequence(self, number=0):
        return "%s %s-%s-%09d" % (self.l10n_latam_document_type_id.doc_code_prefix,
                                    self.journal_id.l10n_ec_entity,
                                    self.journal_id.l10n_ec_emission,
                                    number)

    def _get_starting_sequence(self):
        """ If use documents then will create a new starting sequence using the document type code prefix and the
        journal document number with a 8 padding number """
        if self.journal_id.l10n_latam_use_documents and self.env.company.country_id.code == "EC":
            if self.l10n_latam_document_type_id:
                return self._get_formatted_sequence()
        return super()._get_starting_sequence()