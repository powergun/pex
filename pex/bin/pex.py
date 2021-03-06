# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

"""
The pex.pex utility builds PEX environments and .pex files specified by
sources, requirements and their dependencies.
"""

from __future__ import absolute_import, print_function

import functools
import os
import shutil
import sys
from optparse import OptionGroup, OptionParser, OptionValueError
from textwrap import TextWrapper

from pex.archiver import Archiver
from pex.base import maybe_requirement
from pex.common import die, safe_delete, safe_mkdir, safe_mkdtemp
from pex.crawler import Crawler
from pex.fetcher import Fetcher, PyPIFetcher
from pex.http import Context
from pex.installer import EggInstaller
from pex.interpreter import PythonInterpreter
from pex.iterator import Iterator
from pex.package import EggPackage, SourcePackage
from pex.pex import PEX
from pex.pex_builder import PEXBuilder
from pex.platforms import Platform
from pex.requirements import requirements_from_file
from pex.resolvable import Resolvable
from pex.resolver import Unsatisfiable, resolve_multi
from pex.resolver_options import ResolverOptionsBuilder
from pex.tracer import TRACER
from pex.variables import ENV, Variables
from pex.version import SETUPTOOLS_REQUIREMENT, WHEEL_REQUIREMENT, __version__

CANNOT_DISTILL = 101
CANNOT_SETUP_INTERPRETER = 102
INVALID_OPTIONS = 103
INVALID_ENTRY_POINT = 104


class Logger(object):
  def _default_logger(self, msg, v):
    if v:
      print(msg, file=sys.stderr)

  _LOGGER = _default_logger

  def __call__(self, msg, v):
    self._LOGGER(msg, v)

  def set_logger(self, logger_callback):
    self._LOGGER = logger_callback

log = Logger()


def parse_bool(option, opt_str, _, parser):
  setattr(parser.values, option.dest, not opt_str.startswith('--no'))


def increment_verbosity(option, opt_str, _, parser):
  verbosity = getattr(parser.values, option.dest, 0)
  setattr(parser.values, option.dest, verbosity + 1)


def process_disable_cache(option, option_str, option_value, parser):
  setattr(parser.values, option.dest, None)


def process_pypi_option(option, option_str, option_value, parser, builder):
  if option_str.startswith('--no'):
    setattr(parser.values, option.dest, [])
    builder.clear_indices()
  else:
    indices = getattr(parser.values, option.dest, [])
    pypi = PyPIFetcher()
    if pypi not in indices:
      indices.append(pypi)
    setattr(parser.values, option.dest, indices)
    builder.add_index(PyPIFetcher.PYPI_BASE)


def process_find_links(option, option_str, option_value, parser, builder):
  repos = getattr(parser.values, option.dest, [])
  repo = Fetcher([option_value])
  if repo not in repos:
    repos.append(repo)
  setattr(parser.values, option.dest, repos)
  builder.add_repository(option_value)


def process_index_url(option, option_str, option_value, parser, builder):
  indices = getattr(parser.values, option.dest, [])
  index = PyPIFetcher(option_value)
  if index not in indices:
    indices.append(index)
  setattr(parser.values, option.dest, indices)
  builder.add_index(option_value)


def process_prereleases(option, option_str, option_value, parser, builder):
  if option_str == '--pre':
    builder.allow_prereleases(True)
  elif option_str == '--no-pre':
    builder.allow_prereleases(False)
  else:
    raise OptionValueError


def process_precedence(option, option_str, option_value, parser, builder):
  if option_str == '--build':
    builder.allow_builds()
  elif option_str == '--no-build':
    builder.no_allow_builds()
  elif option_str == '--wheel':
    setattr(parser.values, option.dest, True)
    builder.use_wheel()
  elif option_str in ('--no-wheel', '--no-use-wheel'):
    setattr(parser.values, option.dest, False)
    builder.no_use_wheel()
  else:
    raise OptionValueError


def print_variable_help(option, option_str, option_value, parser):
  for variable_name, variable_type, variable_help in Variables.iter_help():
    print('\n%s: %s\n' % (variable_name, variable_type))
    for line in TextWrapper(initial_indent=' ' * 4, subsequent_indent=' ' * 4).wrap(variable_help):
      print(line)
  sys.exit(0)


def configure_clp_pex_resolution(parser, builder):
  group = OptionGroup(
      parser,
      'Resolver options',
      'Tailor how to find, resolve and translate the packages that get put into the PEX '
      'environment.')

  group.add_option(
      '--pypi', '--no-pypi', '--no-index',
      action='callback',
      dest='repos',
      callback=process_pypi_option,
      callback_args=(builder,),
      help='Whether to use pypi to resolve dependencies; Default: use pypi')

  group.add_option(
    '--pex-path',
    dest='pex_path',
    type=str,
    default=None,
    help='A colon separated list of other pex files to merge into the runtime environment.')

  group.add_option(
      '-f', '--find-links', '--repo',
      metavar='PATH/URL',
      action='callback',
      dest='repos',
      callback=process_find_links,
      callback_args=(builder,),
      type=str,
      help='Additional repository path (directory or URL) to look for requirements.')

  group.add_option(
      '-i', '--index', '--index-url',
      metavar='URL',
      action='callback',
      dest='repos',
      callback=process_index_url,
      callback_args=(builder,),
      type=str,
      help='Additional cheeseshop indices to use to satisfy requirements.')

  group.add_option(
    '--pre', '--no-pre',
    dest='allow_prereleases',
    default=None,
    action='callback',
    callback=process_prereleases,
    callback_args=(builder,),
    help='Whether to include pre-release and development versions of requirements; '
         'Default: only stable versions are used, unless explicitly requested')

  group.add_option(
      '--disable-cache',
      action='callback',
      dest='cache_dir',
      callback=process_disable_cache,
      help='Disable caching in the pex tool entirely.')

  group.add_option(
      '--cache-dir',
      dest='cache_dir',
      default='{pex_root}/build',
      help='The local cache directory to use for speeding up requirement '
           'lookups. [Default: ~/.pex/build]')

  group.add_option(
      '--cache-ttl',
      dest='cache_ttl',
      type=int,
      default=3600,
      help='The cache TTL to use for inexact requirement specifications.')

  group.add_option(
      '--wheel', '--no-wheel', '--no-use-wheel',
      dest='use_wheel',
      default=True,
      action='callback',
      callback=process_precedence,
      callback_args=(builder,),
      help='Whether to allow wheel distributions; Default: allow wheels')

  group.add_option(
      '--build', '--no-build',
      action='callback',
      callback=process_precedence,
      callback_args=(builder,),
      help='Whether to allow building of distributions from source; Default: allow builds')

  # Set the pex tool to fetch from PyPI by default if nothing is specified.
  parser.set_default('repos', [PyPIFetcher()])
  parser.add_option_group(group)


def configure_clp_pex_options(parser):
  group = OptionGroup(
      parser,
      'PEX output options',
      'Tailor the behavior of the emitted .pex file if -o is specified.')

  group.add_option(
      '--zip-safe', '--not-zip-safe',
      dest='zip_safe',
      default=True,
      action='callback',
      callback=parse_bool,
      help='Whether or not the sources in the pex file are zip safe.  If they are '
           'not zip safe, they will be written to disk prior to execution; '
           'Default: zip safe.')

  group.add_option(
      '--always-write-cache',
      dest='always_write_cache',
      default=False,
      action='store_true',
      help='Always write the internally cached distributions to disk prior to invoking '
           'the pex source code.  This can use less memory in RAM constrained '
           'environments. [Default: %default]')

  group.add_option(
      '--ignore-errors',
      dest='ignore_errors',
      default=False,
      action='store_true',
      help='Ignore run-time requirement resolution errors when invoking the pex. '
           '[Default: %default]')

  group.add_option(
      '--inherit-path',
      dest='inherit_path',
      default=False,
      action='store_true',
      help='Inherit the contents of sys.path (including site-packages) running the pex. '
           '[Default: %default]')

  parser.add_option_group(group)


def configure_clp_pex_environment(parser):
  group = OptionGroup(
      parser,
      'PEX environment options',
      'Tailor the interpreter and platform targets for the PEX environment.')

  group.add_option(
      '--python',
      dest='python',
      default=[],
      type='str',
      action='append',
      help='The Python interpreter to use to build the pex.  Either specify an explicit '
           'path to an interpreter, or specify a binary accessible on $PATH. This option '
           'can be passed multiple times to create a multi-interpreter compatible pex. '
           'Default: Use current interpreter.')

  group.add_option(
      '--python-shebang',
      dest='python_shebang',
      default=None,
      help='The exact shebang (#!...) line to add at the top of the PEX file minus the '
           '#!.  This overrides the default behavior, which picks an environment python '
           'interpreter compatible with the one used to build the PEX file.')

  group.add_option(
      '--platform',
      dest='platform',
      default=[],
      type=str,
      action='append',
      help='The platform for which to build the PEX. This option can be passed multiple times '
           'to create a multi-platform compatible pex. Default: current platform.')

  group.add_option(
      '--interpreter-cache-dir',
      dest='interpreter_cache_dir',
      default='{pex_root}/interpreters',
      help='The interpreter cache to use for keeping track of interpreter dependencies '
           'for the pex tool. Default: `~/.pex/interpreters`.')

  parser.add_option_group(group)


def configure_clp_pex_entry_points(parser):
  group = OptionGroup(
      parser,
      'PEX entry point options',
      'Specify what target/module the PEX should invoke if any.')

  group.add_option(
      '-m', '-e', '--entry-point',
      dest='entry_point',
      metavar='MODULE[:SYMBOL]',
      default=None,
      help='Set the entry point to module or module:symbol.  If just specifying module, pex '
           'behaves like python -m, e.g. python -m SimpleHTTPServer.  If specifying '
           'module:symbol, pex imports that symbol and invokes it as if it were main.')

  group.add_option(
      '-c', '--script', '--console-script',
      dest='script',
      default=None,
      metavar='SCRIPT_NAME',
      help='Set the entry point as to the script or console_script as defined by a any of the '
           'distributions in the pex.  For example: "pex -c fab fabric" or "pex -c mturk boto".')

  parser.add_option_group(group)


def configure_clp():
  usage = (
      '%prog [-o OUTPUT.PEX] [options] [-- arg1 arg2 ...]\n\n'
      '%prog builds a PEX (Python Executable) file based on the given specifications: '
      'sources, requirements, their dependencies and other options.')

  parser = OptionParser(usage=usage, version='%prog {0}'.format(__version__))
  resolver_options_builder = ResolverOptionsBuilder()
  configure_clp_pex_resolution(parser, resolver_options_builder)
  configure_clp_pex_options(parser)
  configure_clp_pex_environment(parser)
  configure_clp_pex_entry_points(parser)

  parser.add_option(
      '-o', '--output-file',
      dest='pex_name',
      default=None,
      help='The name of the generated .pex file: Omiting this will run PEX '
           'immediately and not save it to a file.')

  parser.add_option(
      '-p', '--preamble-file',
      dest='preamble_file',
      metavar='FILE',
      default=None,
      type=str,
      help='The name of a file to be included as the preamble for the generated .pex file')

  parser.add_option(
      '-r', '--requirement',
      dest='requirement_files',
      metavar='FILE',
      default=[],
      type=str,
      action='append',
      help='Add requirements from the given requirements file.  This option can be used multiple '
           'times.')

  parser.add_option(
      '--constraints',
      dest='constraint_files',
      metavar='FILE',
      default=[],
      type=str,
      action='append',
      help='Add constraints from the given constraints file.  This option can be used multiple '
           'times.')

  parser.add_option(
      '-v',
      dest='verbosity',
      default=0,
      action='callback',
      callback=increment_verbosity,
      help='Turn on logging verbosity, may be specified multiple times.')

  parser.add_option(
      '--pex-root',
      dest='pex_root',
      default=None,
      help='Specify the pex root used in this invocation of pex. [Default: ~/.pex]'
  )

  parser.add_option(
      '--help-variables',
      action='callback',
      callback=print_variable_help,
      help='Print out help about the various environment variables used to change the behavior of '
           'a running PEX file.')

  return parser, resolver_options_builder


def _safe_link(src, dst):
  try:
    os.unlink(dst)
  except OSError:
    pass
  os.symlink(src, dst)


def _resolve_and_link_interpreter(requirement, fetchers, target_link, installer_provider):
  # Short-circuit if there is a local copy
  if os.path.exists(target_link) and os.path.exists(os.path.realpath(target_link)):
    egg = EggPackage(os.path.realpath(target_link))
    if egg.satisfies(requirement):
      return egg

  context = Context.get()
  iterator = Iterator(fetchers=fetchers, crawler=Crawler(context))
  links = [link for link in iterator.iter(requirement) if isinstance(link, SourcePackage)]

  with TRACER.timed('Interpreter cache resolving %s' % requirement, V=2):
    for link in links:
      with TRACER.timed('Fetching %s' % link, V=3):
        sdist = context.fetch(link)

      with TRACER.timed('Installing %s' % link, V=3):
        installer = installer_provider(sdist)
        dist_location = installer.bdist()
        target_location = os.path.join(
            os.path.dirname(target_link), os.path.basename(dist_location))
        shutil.move(dist_location, target_location)
        _safe_link(target_location, target_link)

      return EggPackage(target_location)


def resolve_interpreter(cache, fetchers, interpreter, requirement):
  """Resolve an interpreter with a specific requirement.

     Given a :class:`PythonInterpreter` and a requirement, return an
     interpreter with the capability of resolving that requirement or
     ``None`` if it's not possible to install a suitable requirement."""
  requirement = maybe_requirement(requirement)

  # short circuit
  if interpreter.satisfies([requirement]):
    return interpreter

  def installer_provider(sdist):
    return EggInstaller(
        Archiver.unpack(sdist),
        strict=requirement.key != 'setuptools',
        interpreter=interpreter)

  interpreter_dir = os.path.join(cache, str(interpreter.identity))
  safe_mkdir(interpreter_dir)

  egg = _resolve_and_link_interpreter(
      requirement,
      fetchers,
      os.path.join(interpreter_dir, requirement.key),
      installer_provider)

  if egg:
    return interpreter.with_extra(egg.name, egg.raw_version, egg.path)


def get_interpreter(python_interpreter, interpreter_cache_dir, repos, use_wheel):
  interpreter = None

  if python_interpreter:
    if os.path.exists(python_interpreter):
      interpreter = PythonInterpreter.from_binary(python_interpreter)
    else:
      interpreter = PythonInterpreter.from_env(python_interpreter)
    if interpreter is None:
      die('Failed to find interpreter: %s' % python_interpreter)
  else:
    interpreter = PythonInterpreter.get()

  with TRACER.timed('Setting up interpreter %s' % interpreter.binary, V=2):
    resolve = functools.partial(resolve_interpreter, interpreter_cache_dir, repos)

    # resolve setuptools
    interpreter = resolve(interpreter, SETUPTOOLS_REQUIREMENT)

    # possibly resolve wheel
    if interpreter and use_wheel:
      interpreter = resolve(interpreter, WHEEL_REQUIREMENT)

    return interpreter


def _lowest_version_interpreter(interpreters):
  """Given a list of interpreters, return the one with the lowest version."""
  lowest = interpreters[0]
  for i in interpreters[1:]:
    lowest = lowest if lowest < i else i
  return lowest


def build_pex(args, options, resolver_option_builder):
  with TRACER.timed('Resolving interpreters', V=2):
    interpreters = [
      get_interpreter(interpreter,
                      options.interpreter_cache_dir,
                      options.repos,
                      options.use_wheel)
      for interpreter in options.python or [None]
    ]

  if not interpreters:
    die('Could not find compatible interpreter', CANNOT_SETUP_INTERPRETER)

  try:
    with open(options.preamble_file) as preamble_fd:
      preamble = preamble_fd.read()
  except TypeError:
    # options.preamble_file is None
    preamble = None

  interpreter = _lowest_version_interpreter(interpreters)
  pex_builder = PEXBuilder(path=safe_mkdtemp(), interpreter=interpreter, preamble=preamble)

  pex_info = pex_builder.info
  pex_info.zip_safe = options.zip_safe
  pex_info.pex_path = options.pex_path
  pex_info.always_write_cache = options.always_write_cache
  pex_info.ignore_errors = options.ignore_errors
  pex_info.inherit_path = options.inherit_path

  resolvables = [Resolvable.get(arg, resolver_option_builder) for arg in args]

  for requirements_txt in options.requirement_files:
    resolvables.extend(requirements_from_file(requirements_txt, resolver_option_builder))

  # pip states the constraints format is identical tor requirements
  # https://pip.pypa.io/en/stable/user_guide/#constraints-files
  for constraints_txt in options.constraint_files:
    constraints = []
    for r in requirements_from_file(constraints_txt, resolver_option_builder):
      r.is_constraint = True
      constraints.append(r)
    resolvables.extend(constraints)

  with TRACER.timed('Resolving distributions'):
    try:
      resolveds = resolve_multi(resolvables,
                                interpreters=interpreters,
                                platforms=options.platform,
                                cache=options.cache_dir,
                                cache_ttl=options.cache_ttl,
                                allow_prereleases=resolver_option_builder.prereleases_allowed)

      for dist in resolveds:
        log('  %s' % dist, v=options.verbosity)
        pex_builder.add_distribution(dist)
        pex_builder.add_requirement(dist.as_requirement())
    except Unsatisfiable as e:
      die(e)

  if options.entry_point and options.script:
    die('Must specify at most one entry point or script.', INVALID_OPTIONS)

  if options.entry_point:
    pex_builder.set_entry_point(options.entry_point)
  elif options.script:
    pex_builder.set_script(options.script)

  if options.python_shebang:
    pex_builder.set_shebang(options.python_shebang)

  return pex_builder


def make_relative_to_root(path):
  """Update options so that defaults are user relative to specified pex_root."""
  return os.path.normpath(path.format(pex_root=ENV.PEX_ROOT))


def main(args=None):
  args = args or sys.argv[1:]
  parser, resolver_options_builder = configure_clp()

  try:
    separator = args.index('--')
    args, cmdline = args[:separator], args[separator + 1:]
  except ValueError:
    args, cmdline = args, []

  options, reqs = parser.parse_args(args=args)
  if options.pex_root:
    ENV.set('PEX_ROOT', options.pex_root)
  else:
    options.pex_root = ENV.PEX_ROOT  # If option not specified fallback to env variable.

  # Don't alter cache if it is disabled.
  if options.cache_dir:
    options.cache_dir = make_relative_to_root(options.cache_dir)
  options.interpreter_cache_dir = make_relative_to_root(options.interpreter_cache_dir)

  with ENV.patch(PEX_VERBOSE=str(options.verbosity)):
    with TRACER.timed('Building pex'):
      pex_builder = build_pex(reqs, options, resolver_options_builder)

    if options.pex_name is not None:
      log('Saving PEX file to %s' % options.pex_name, v=options.verbosity)
      tmp_name = options.pex_name + '~'
      safe_delete(tmp_name)
      pex_builder.build(tmp_name)
      os.rename(tmp_name, options.pex_name)
      return 0

    if options.platform and Platform.current() not in options.platform:
      log('WARNING: attempting to run PEX with incompatible platforms!')

    pex_builder.freeze()

    log('Running PEX file at %s with args %s' % (pex_builder.path(), cmdline), v=options.verbosity)
    pex = PEX(pex_builder.path(), interpreter=pex_builder.interpreter)
    sys.exit(pex.run(args=list(cmdline)))


if __name__ == '__main__':
  main()
