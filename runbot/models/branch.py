# -*- coding: utf-8 -*-
import logging
import re
from subprocess import CalledProcessError
from odoo import models, fields, api

_logger = logging.getLogger(__name__)
_re_coverage = re.compile(r'\bcoverage\b')
_re_patch = re.compile(r'.*patch-\d+$')

class runbot_branch(models.Model):

    _name = "runbot.branch"
    _order = 'name'
    _sql_constraints = [('branch_repo_uniq', 'unique (name,repo_id)', 'The branch must be unique per repository !')]

    repo_id = fields.Many2one('runbot.repo', 'Repository', required=True, ondelete='cascade')
    name = fields.Char('Ref Name', required=True)
    branch_name = fields.Char(compute='_get_branch_infos', string='Branch', readonly=1, store=True)
    branch_url = fields.Char(compute='_get_branch_url', string='Branch url', readonly=1)
    pull_head_name = fields.Char(compute='_get_branch_infos', string='PR HEAD name', readonly=1, store=True)
    target_branch_name = fields.Char(compute='_get_branch_infos', string='PR target branch', readonly=1, store=True)
    sticky = fields.Boolean('Sticky')
    coverage = fields.Boolean('Coverage')
    coverage_result = fields.Float(compute='_get_last_coverage', type='Float', string='Last coverage', store=False)
    state = fields.Char('Status')
    modules = fields.Char("Modules to Install", help="Comma-separated list of modules to install and test.")
    job_timeout = fields.Integer('Job Timeout (minutes)', help='For default timeout: Mark it zero')
    priority = fields.Boolean('Build priority', default=False)
    job_type = fields.Selection([
        ('testing', 'Testing jobs only'),
        ('running', 'Running job only'),
        ('all', 'All jobs'),
        ('none', 'Do not execute jobs')
    ], required=True, default='all')

    @api.depends('name')
    def _get_branch_infos(self):
        """compute branch_name, branch_url, pull_head_name and target_branch_name based on name"""
        for branch in self:
            if branch.name:
                branch.branch_name = branch.name.split('/')[-1]
                pi = branch._get_pull_info()
                if pi:
                    branch.target_branch_name = pi['base']['ref']
                    if not _re_patch.match(pi['head']['label']):
                        # label is used to disambiguate PR with same branch name
                        branch.pull_head_name = pi['head']['label']

    @api.depends('branch_name')
    def _get_branch_url(self):
        """compute the branch url based on branch_name"""
        for branch in self:
            if branch.name:
                if re.match('^[0-9]+$', branch.branch_name):
                    branch.branch_url = "https://%s/pull/%s" % (branch.repo_id.base, branch.branch_name)
                else:
                    branch.branch_url = "https://%s/tree/%s" % (branch.repo_id.base, branch.branch_name)

    def _get_pull_info(self):
        self.ensure_one()
        repo = self.repo_id
        if repo.token and self.name.startswith('refs/pull/'):
            pull_number = self.name[len('refs/pull/'):]
            return repo._github('/repos/:owner/:repo/pulls/%s' % pull_number, ignore_errors=True) or {}
        return {}

    def _is_on_remote(self):
        # check that a branch still exists on remote
        self.ensure_one()
        branch = self
        repo = branch.repo_id
        try:
            repo._git(['ls-remote', '-q', '--exit-code', repo.name, branch.name])
        except CalledProcessError:
            return False
        return True

    def create(self, vals):
        vals.setdefault('coverage', _re_coverage.search(vals.get('name') or '') is not None)
        return super(runbot_branch, self).create(vals)

    def _get_branch_quickconnect_url(self, fqdn, dest):
        self.ensure_one()
        r = {}
        r[self.id] = "http://%s/web/login?db=%s-all&login=admin&redirect=/web?debug=1" % (fqdn, dest)
        return r

    def _get_last_coverage_build(self):
        """ Return the last build with a coverage value > 0"""
        self.ensure_one()
        return self.env['runbot.build'].search([
                ('branch_id.id', '=', self.id),
                ('state', 'in', ['done', 'running']),
                ('coverage_result', '>=', 0.0),
            ], order='sequence desc', limit=1)

    def _get_last_coverage(self):
        """ Compute the coverage result of the last build in branch """
        for branch in self:
            last_build = branch._get_last_coverage_build()
            branch.coverage_result = last_build.coverage_result or 0.0

    def _get_closest_branch(self, target_repo_id):
        """
        """
        self.ensure_one()
        Branch = self.env['runbot.branch']

        repo = self.repo_id
        name = self.pull_head_name or self.branch_name

        target_repo = self.env['runbot.repo'].browse(target_repo_id)

        target_repo_ids = [target_repo.id]
        r = target_repo.duplicate_id  # find it also in odoo/dev when searching in odoo? we could do better
        while r:
            if r.id in target_repo_ids:
                break
            target_repo_ids.append(r.id)
            r = r.duplicate_id

        _logger.debug('Search closest of %s (%s) in repos %r', name, repo.name, target_repo_ids)

        sort_by_repo = lambda d: (not d.sticky,      # sticky first
                                  target_repo_ids.index(d.repo_id[0].id),
                                  -1 * d.branch_name,  # little change of logic here, is it ok?
                                  -1 * d.id)
        result_for = lambda d, match='exact': (d['repo_id'][0].id, d['name'], match)

        # 1. same name, not a PR
        if not self.pull_head_name:  # not a pr
            domain = [
                ('repo_id', 'in', target_repo_ids),
                ('branch_name', '=', self.branch_name),
                ('name', '=like', 'refs/heads/%'),
            ]
            targets = Branch.search(domain, order='id DESC')
            targets = sorted(targets, key=sort_by_repo)
            if targets and self._branch_exists(targets[0]['id']):
                return (targets[0], 'exact')

        # 2. PR with head name equals
        # is it possible that pull exists but branch not (in db) ?
        if self.pull_head_name:  # a pr? to check, possible that we have no pull_head_name if it is an external pr
            domain = [
                ('repo_id', 'in', target_repo_ids),
                ('pull_head_name', '=', self.pull_head_name),
                ('name', '=like', 'refs/pull/%'),
            ]
            pulls = Branch.search(domain, order='id DESC')
            pulls = sorted(pulls, key=sort_by_repo)
            for pull in Branch.browse([pu['id'] for pu in pulls]):
                pi = pull._get_pull_info()
                if pi.get('state') == 'open':
                    if ':' in self.pull_head_name:  # we assume that branch exists if we got pull info
                        pr_branch_name = self.pull_head_name.split(':')[1]
                        pr_branch_ref = 'refs/heads/%s' % pr_branch_name
                        pr_branch = self.search([('repo_id', '=', pull.repo_id.duplicate_id.id), ('name', '=', pr_branch_ref)])
                        if pr_branch:
                            return (pr_branch, 'exact PR')
                    return (pull, 'exact PR')

        # 3. Match a branch which is the dashed-prefix of current branch name
        if '-' in name:  # not a pr ? or may be a pr ?
            # before it could have matche if pr pull_head_name was starting with branch
            name_start = 'refs/heads/%s' % name.split('-')[0]
            domain = [('repo_id', 'in', target_repo_ids), ('name', '=like', '%s%%' % name_start)]
            branches = Branch.search(domain, order='id DESC')
            branches = sorted(branches, key=sort_by_repo)
            for branch in branches:
                # shouldn't we only match on sticky?
                if name.startswith('%s-' % branch.branch_name) and self._branch_exists(branch.id):
                    return (branch, 'prefix')

        # 4.Match a PR in enterprise without community PR
        if self.pull_head_name:
            if self.name.startswith('refs/pull') and ':' in name:
                pr_branch_name = name.split(':')[1]
                duplicate_branch_name = 'refs/heads/%s' % pr_branch_name
                domain = [
                    ('repo_id', 'in', target_repo_ids),  # target_repo_ids should contain the target duplicate repo
                    ('branch_name', '=', pr_branch_name),
                    ('pull_head_name', '=', False),
                ]
                targets = Branch.search(domain, order='id DESC')
                targets = sorted(targets, key=sort_by_repo)
                if targets and self._branch_exists(targets[0].id):
                    return (targets[0], 'no PR')

        # 5. last-resort value
        default_repo_id = target_repo.id
        target_branch_name = 'master'

        if self.target_branch_name:
            target_branch_name = self.target_branch_name
        else:
            default_repo_id = target_repo.duplicate_id

        target_ref = 'refs/heads/%s' % target_branch_name
        default_branch = self.search([('repo_id', 'in', target_repo_ids), ('name', '=', target_ref)])
        # assert branch
        return (default_branch, 'default')

    def _branch_exists(self, branch_id):
        Branch = self.env['runbot.branch']
        branch = Branch.search([('id', '=', branch_id)])
        if branch and branch[0]._is_on_remote():
            return True
        return False
