# -*- encoding: utf-8 -*-

import glob
import io
import logging
import re
import time
import os

from odoo.addons.runbot.models.build import runbot_job, _re_error, _re_warning, re_job
from odoo import models, fields, api, _
from odoo.addons.runbot.container import docker_build, docker_run
from odoo.addons.runbot.common import dt2time, fqdn, now, grep, time2str, rfind, uniq_list, local_pgadmin_cursor, get_py_version



_logger = logging.getLogger(__name__)


class RunbotJob(models.Model):
    _name = "runbot.job"

    name = fields.Char(required=True)
    logs_location = fields.Char(string="Log file location")
    logs_name = fields.Char(string="Log name")

    is_default_parsed = fields.Boolean(default=False)

class runbot_repo(models.Model):
    _inherit = "runbot.repo"

    nobuild = fields.Boolean(default=False)
    skip_job_ids = fields.Many2many('runbot.job', 'runbot_job_runbot_repo_skip_rel', string='Jobs to skip')
    parse_job_ids = fields.Many2many('runbot.job', 'runbot_job_runbot_repo_parse_rel', string='Jobs to parse', default=lambda self: self.env['runbot.job'].search([('is_default_parsed', '=', True)]))
    restored_db_name = fields.Char(string='Database name to replicated')
    force_update_all = fields.Boolean('Force Update ALL', help='Force update all on job_26 otherwise it will update only the modules in the repository', default=False)
    testenable_job26 = fields.Boolean('Test enable on upgrade', help='test enabled on update of the restored database', default=False)

class runbot_branch(models.Model):
    _inherit = "runbot.branch"

    def _get_branch_quickconnect_url(self, fqdn, dest):
        self.ensure_one()
        if self.repo_id.restored_db_name:
            r = {}
            r[self.id] = "http://%s/web/login?db=%s-custom&login=admin&redirect=/web?debug=1" % (
                fqdn, dest)
        else:
            r = super(runbot_branch, self)._get_branch_quickconnect_url(
                fqdn, dest)
        return r

class runbot_build(models.Model):
    _inherit = "runbot.build"

    restored_db_name = fields.Char(string='Database name to replicated')

    def create(self, vals):
        build_id = super(runbot_build, self).create(vals)
        if build_id.repo_id.restored_db_name:
            build_id.write(
                {'restored_db_name': build_id.repo_id.restored_db_name})
        if build_id.repo_id.nobuild:
            build_id.write({'state': 'done'})
        return build_id

    def _list_jobs(self):
        all_jobs = super(runbot_build, self)._list_jobs()
        jobs = self._clean_jobs(all_jobs)
        return jobs

    def _clean_jobs(self, jobs):
        self.ensure_one()
        jobs = jobs[:]
        for job_to_skip in self.repo_id.skip_job_ids:
            jobs.remove(job_to_skip.name)
        return jobs

    @runbot_job('testing', 'running')
    def _job_29_results(self, build, log_path):
        files_to_parse = build.repo_id.parse_job_ids.mapped('name')
        build._log('run', 'Getting results for build %s' % build.dest)
        v = {}
        result = []
        for file in files_to_parse:
            log_all = build._path('logs', file)
            log_time = time.localtime(os.path.getmtime(log_all))
            v['job_end'] = time2str(log_time)
            if grep(log_all, ".modules.loading: Modules loaded."):
                if rfind(log_all, _re_error):
                    result.append("ko")
                elif rfind(log_all, _re_warning):
                    result.append("warn")
                elif not grep(build._server("test/common.py"), "post_install") or grep(log_all, "Initiating shutdown."):
                    result.append("ok")
            else:
                result.append("ko")
        if 'ko' in result:
            v['result'] = 'ko'
        elif 'warn' in result:
            v['result'] = 'warn'
        else:
            v['result'] = 'ok'
        build.write(v)
        build._github_status()
        return -2

    @runbot_job('testing', 'running')
    def _job_25_restore(self, build, log_path):
        if not build.restored_db_name:
            return -2
        self._local_pg_createdb("%s-custom" % build.dest)
        build._log('restore', 'Restoring %s on %s-custom' %
                   (build.restored_db_name, build.dest))
        cmd = "pg_dump %s | psql %s-custom" % (
            build.restored_db_name, build.dest)
        # TODO: avoid launching odoo-bin in docker_run
        return docker_run(cmd, log_path, build._path(), build._get_docker_name())

    @runbot_job('testing', 'running')
    def _job_26_upgrade(self, build, log_path):
        if not build.restored_db_name:
            return -2
        to_test = build.modules if build.modules and not build.repo_id.force_update_all else 'all'
        cmd, mods = build._cmd()
        build._log('upgrade', 'Start Upgrading %s modules on %s-custom' % (to_test, build.dest))
        cmd += ['-d', '%s-custom' % build.dest, '-u', to_test, '--stop-after-init', '--log-level=info']
        if build.repo_id.testenable_job26:
            cmd.append("--test-enable")
        return docker_run(cmd, log_path, build._path(), build._get_docker_name())
