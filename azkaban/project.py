#!/usr/bin/env python
# encoding: utf-8

"""Project definition module.

Sample usage:

.. code:: python

  from azkaban import Job, Project

  project = Project('foo')

  project.add_job('bar', Job())
  project.add_file('/some/file.path')

  if __name__ == '__main__':
    project.main()

"""

from collections import defaultdict
from ConfigParser import RawConfigParser
from docopt import docopt
from getpass import getpass, getuser
from os import sep
from os.path import abspath, exists, expanduser, getsize, isabs, relpath
from requests import post, ConnectionError
from requests.exceptions import MissingSchema
from sys import argv, exit, stdout
from zipfile import ZipFile

from . import __doc__ as azkaban_doc, __version__
from .util import AzkabanError, human_readable, pretty_print, temppath

import logging

logger = logging.getLogger(__name__)


class Project(object):

  """Azkaban project.

  :param name: name of the project

  """

  rcpath = expanduser('~/.azkabanrc')

  def __init__(self, name):
    self.name = name
    self._jobs = {}
    self._files = {}

  def add_file(self, path, archive_path=None):
    """Include a file in the project archive.

    :param path: absolute path to file
    :param archive_path: path to file in archive (defaults to same as `path`)

    This method requires the path to be absolute to avoid having files in the
    archive with lower level destinations than the base root directory.

    """
    logger.debug('adding file %r as %r', path, archive_path or path)
    if not isabs(path):
      raise AzkabanError('relative path not allowed %r' % (path, ))
    elif path in self._files:
      if self._files[path] != archive_path:
        raise AzkabanError('inconsistent duplicate %r' % (path, ))
    else:
      if not exists(path):
        raise AzkabanError('missing file %r' % (path, ))
      self._files[path] = archive_path

  def add_job(self, name, job):
    """Include a job in the project.

    :param name: name assigned to job (must be unique)
    :param job: `Job` subclass

    This method triggers the `on_add` method on the added job (passing the
    project and name as arguments). The handler will be called right after the
    job is added.

    """
    logger.debug('adding job %r', name)
    if name in self._jobs:
      raise AzkabanError('duplicate job name %r' % (name, ))
    else:
      self._jobs[name] = job
      job.on_add(self, name)

  def merge_into(self, project):
    """Merge one project with another.

    :param project: project to merge with this project

    This method does an in place merge of the current project with another.
    The merged project will maintain the current project's name.
    """
    logger.debug('merging into project %r', project.name)
    for name, job in self._jobs.items():
      project.add_job(name, job)
    for path, archive_path in self._files.items():
      project.add_file(path, archive_path)

  def build(self, path, overwrite=False):
    """Create the project archive.

    :param path: destination path
    :param overwrite: don't throw an error if a file already exists at `path`

    Triggers the `on_build` method on each job inside the project (passing
    itself and the job's name as two argument). This method will be called
    right before the job file is generated.

    """
    logger.debug('building project')
    # not using a with statement for compatibility with older python versions
    if exists(path) and not overwrite:
      raise AzkabanError('path %r already exists' % (path, ))
    if not (len(self._jobs) or len(self._files)):
      raise AzkabanError('building empty project')
    writer = ZipFile(path, 'w')
    try:
      for name, job in self._jobs.items():
        job.on_build(self, name)
        with temppath() as fpath:
          job.build(fpath)
          writer.write(fpath, '%s.job' % (name, ))
      for fpath, apath in self._files.items():
        writer.write(fpath, apath)
    finally:
      writer.close()
    size = human_readable(getsize(path))
    logger.info('project successfully built (size: %s)' % (size, ))

  def upload(self, archive, url=None, user=None, password=None, alias=None):
    """Build and upload project to Azkaban.

    :param archive: path to zip file (typically the output of `build`)
    :param url: http endpoint URL (including protocol)
    :param user: Azkaban username (must have the appropriate permissions)
    :param password: Azkaban login password
    :param alias: section of rc file used to cache URLs (will enable session
      ID caching)

    Note that in order to upload to Azkaban, the project must have already been
    created and the corresponding user must have permissions to upload.

    """
    (url, session_id) = self._get_credentials(url, user, password, alias)
    logger.debug('uploading project to %r', url)
    try:
      req = post(
        '%s/manager' % (url, ),
        data={
          'ajax': 'upload',
          'session.id': session_id,
          'project': self.name,
        },
        files={
          'file': ('file.zip', open(archive, 'rb'), 'application/zip'),
        },
        verify=False
      )
    except ConnectionError:
      raise AzkabanError('unable to connect to azkaban server')
    except MissingSchema:
      raise AzkabanError('invalid azkaban server url')
    except IOError:
      raise AzkabanError('unable to find archive at %r' % (archive, ))
    else:
      res = req.json()
      if 'error' in res:
        raise AzkabanError(res['error'])
      else:
        logger.info(
          'project successfully uploaded (id: %s, version: %s)' %
          (res['projectId'], res['version'])
        )
        return res

  def run(self, flow, url=None, user=None, password=None, alias=None):
    """Run a workflow on Azkaban.

    :param flow: name of the workflow
    :param url: http endpoint URL (including protocol)
    :param user: Azkaban username (must have the appropriate permissions)
    :param password: Azkaban login password
    :param alias: section of rc file used to cache URLs (will enable session
      ID caching)

    Note that in order to run a workflow on Azkaban, it must already have been
    uploaded and the corresponding user must have permissions to run.

    """
    (url, session_id) = self._get_credentials(url, user, password, alias)
    logger.debug('running flow %s on %r', flow, url)
    try:
      req = post(
        '%s/executor' % (url, ),
        data={
          'ajax': 'executeFlow',
          'session.id': session_id,
          'project': self.name,
          'flow': flow,
        },
        verify=False,
      )
    except ConnectionError:
      raise AzkabanError('unable to connect to azkaban server')
    except MissingSchema:
      raise AzkabanError('invalid azkaban server url')
    else:
      res = req.json()
      if 'error' in res:
        raise AzkabanError(res['error'])
      else:
        logger.info(
          'successfully started flow %s (execution id: %s)' %
          (flow, res['execid'])
        )
        logger.info(
          'details at %s/executor?execid=%s' % (url, res['execid'])
        )
        return res

  def main(self):
    """Command line argument parser."""
    argv.insert(0, 'FILE')
    args = docopt(azkaban_doc, version=__version__)
    if not args['--quiet']:
      logger.setLevel(logging.INFO)
      logger.addHandler(get_formatted_stream_handler())
    try:
      if args['build']:
        self.build(args['PATH'], overwrite=args['--overwrite'])
      elif args['upload']:
        if args['--zip']:
          self.upload(
            args['--zip'],
            url=args['URL'],
            user=args['--user'],
            alias=args['--alias'],
          )
        else:
          with temppath() as path:
            self.build(path)
            self.upload(
              path,
              url=args['URL'],
              user=args['--user'],
              alias=args['--alias'],
            )
      elif args['run']:
        self.run(
          flow=args['FLOW'],
          url=args['URL'],
          user=args['--user'],
          alias=args['--alias'],
        )
      elif args['view']:
        job_name = args['JOB']
        if job_name in self._jobs:
          job = self._jobs[job_name]
          pretty_print(job.build_options)
        else:
          raise AzkabanError('missing job %r' % (job_name, ))
      elif args['list']:
        jobs = defaultdict(list)
        if args['--pretty']:
          if args['--files']:
            for path, apath in self._files.items():
              rpath = relpath(path)
              size = human_readable(getsize(path))
              apath = apath or abspath(path).lstrip(sep)
              stdout.write('%s: %s [%s]\n' % (rpath, size, apath))
          else:
            for name, job in self._jobs.items():
              job_type = job.build_options.get('type', '--')
              job_deps = job.build_options.get('dependencies', '')
              if job_deps:
                info = '%s [%s]' % (name, job_deps)
              else:
                info = name
              jobs[job_type].append(info)
            pretty_print(jobs)
        else:
          if args['--files']:
            for path in self._files:
              stdout.write('%s\n' % (relpath(path), ))
          else:
            for name in self._jobs:
              stdout.write('%s\n' % (name, ))
    except AzkabanError as err:
      logger.error(err)
      exit(1)

  def _get_credentials(self, url=None, user=None, password=None, alias=None):
    """Get valid session ID.

    :param url: http endpoint (including port)
    :param user: username which will be used to upload the built project
      (defaults to the current user)
    :param password: password used to log into Azkaban
    :param alias: alias name used to find the URL, and an existing
      session ID if possible (will override the URL parameter)

    """
    if alias:
      parser = RawConfigParser({'user': '', 'session_id': ''})
      parser.read(self.rcpath)
      if not parser.has_section(alias):
        raise AzkabanError('missing alias %r' % (alias, ))
      elif not parser.has_option(alias, 'url'):
        raise AzkabanError('missing url for alias %r' % (alias, ))
      else:
        url = parser.get(alias, 'url')
        user = parser.get(alias, 'user')
        session_id = parser.get(alias, 'session_id')
    elif url:
      session_id = None
    else:
      raise ValueError('Either url or alias must be specified.')
    url = url.rstrip('/')
    if not session_id or post(
      '%s/manager' % (url, ),
      {'session.id': session_id},
      verify=False
    ).text:
      user = user or getuser()
      password = password or getpass('azkaban password for %s: ' % (user, ))
      try:
        req = post(
          url,
          data={'action': 'login', 'username': user, 'password': password},
          verify=False,
        )
      except ConnectionError:
        raise AzkabanError('unable to connect to azkaban server')
      except MissingSchema:
        raise AzkabanError('invalid azkaban server url')
      else:
        res = req.json()
        if 'error' in res:
          raise AzkabanError(res['error'])
        else:
          session_id = res['session.id']
          if alias:
            parser.set(alias, 'session_id', session_id)
            with open(self.rcpath, 'w') as writer:
              parser.write(writer)
    return (url, session_id)
