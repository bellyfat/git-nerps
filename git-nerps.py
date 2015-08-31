#!/usr/bin/env python2
# -*- coding: utf-8 -*-
from __future__ import print_function

import itertools as it, operator as op, functools as ft
from contextlib import contextmanager
from os.path import ( join, expanduser, isdir,
	realpath, dirname, abspath, exists, samefile, normpath )
import os, sys, io, re, types, logging
import stat, tempfile, fcntl, subprocess
import hmac, hashlib


class Conf(object):

	key_name_pool = [ # NATO phonetic alphabet
		'dash', 'alfa', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf',
		'hotel', 'india', 'juliett', 'kilo', 'lima', 'mike', 'november', 'oscar',
		'papa', 'quebec', 'romeo', 'sierra', 'tango', 'uniform', 'victor',
		'whiskey', 'x-ray', 'yankee', 'zulu' ]

	umask = 0700 # for files where keys are stored

	script_link = '~/.git-nerps'
	git_conf_home = '~/.git-nerps-keys'
	git_conf_version = 1

	enc_watermark = '¯\_ʻnerpsʻ_/¯'

	def nonce_func(self, plaintext):
		raw = hmac.new(self.enc_watermark, plaintext, hashlib.sha256).digest()
		return raw[:self.nacl.SecretBox.NONCE_SIZE]

	def __init__(self, nacl): self.nacl = nacl
	def __repr__(self): return repr(vars(self))
	def get(self, *k): return getattr(self, '_'.join(k))


class NaCl(object):

	imports = dict(
		exceptions=['CryptoError', 'BadSignatureError'],
		encoding=['RawEncoder', 'URLSafeBase64Encoder'],
		secret=['SecretBox'], hash=['sha256'], utils=['random'] )

	def __init__(self):
		import warnings, importlib
		with warnings.catch_warnings(record=True): # cffi warnings
			for mod, keys in self.imports.viewitems():
				mod = importlib.import_module('nacl.{}'.format(mod))
				for k in keys: setattr(self, k, getattr(mod, k))

	def key_encode(self, key):
		return key.encode(self.URLSafeBase64Encoder)

	def key_decode(self, key_str, name=None, t=None, raw=False):
		enc = self.URLSafeBase64Encoder if not raw else self.RawEncoder
		key = (t or self.SecretBox)(key_str, enc)
		if name: key.name = name
		return key


@contextmanager
def safe_replacement(path, mode=None):
	if mode is None:
		try: mode = stat.S_IMODE(os.lstat(path).st_mode)
		except (OSError, IOError): pass
	kws = dict( delete=False,
		dir=os.path.dirname(path), prefix=os.path.basename(path)+'.' )
	with tempfile.NamedTemporaryFile(**kws) as tmp:
		try:
			if mode is not None: os.fchmod(tmp.fileno(), mode)
			yield tmp
			if not tmp.closed: tmp.flush()
			os.rename(tmp.name, path)
		except CancelFileReplacement: pass
		finally:
			try: os.unlink(tmp.name)
			except (OSError, IOError): pass

class CancelFileReplacement(Exception): pass
safe_replacement.cancel = CancelFileReplacement

@contextmanager
def edit(path):
	with safe_replacement(path) as tmp:
		if not exists(path): yield None, tmp
		else:
			with open(path, 'rb') as src: yield src, tmp

edit.cancel = CancelFileReplacement

def with_src_lock(shared=False):
	lock = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
	def _decorator(func):
		@ft.wraps(func)
		def _wrapper(src, *args, **kws):
			fcntl.lockf(src, lock)
			try: return func(src, *args, **kws)
			finally:
				try: fcntl.lockf(src, fcntl.LOCK_UN)
				except (OSError, IOError) as err:
					log.exception('Failed to unlock file object: %s', err)
		return _wrapper
	return _decorator

def relpath(path, from_path):
	path, from_path = it.imap(abspath, (path, from_path))
	if isdir(from_path): from_path += os.sep
	from_path = dirname(from_path)
	path, from_path = it.imap(lambda x: x.split(os.sep), (path, from_path))
	for i in xrange(min(len(from_path), len(path))):
		if from_path[i] != path[i]: break
		else: i +=1
	return join(*([os.pardir] * (len(from_path)-i) + path[i:]))

def path_escape(path):
	assert path.strip() == path, repr(path) # trailing spaces should be escaped
	for c in '#!':
		if path.startswith(c): path = r'\{}{}'.format(c, path[1:])
	return path.replace('*', r'\*')

def filter_git_patterns(src, tmp, path_rel, _ree=re.escape):
	if not src: src = io.BytesIO()
	for n, line in enumerate(iter(src.readline, ''), 1):
		ls = line.strip()
		assert not ls.endswith('\\'), repr(line) # not handling these escapes
		if ls and not ls.startswith('#'):
			pat, filters = ls.split(None, 1)
			pat_re = _ree(pat.lstrip('/')).replace(_ree('**'), r'(.+)').replace(_ree('*'), r'([^\/]+)')
			if '/' in pat: pat_re = '^{}'.format(pat_re)
			if re.search(pat_re, path_rel):
				act = yield n, line, pat, filters
		if not act: tmp.write('{}\n'.format(line.rstrip()))
		elif isinstance(act, bytes): tmp.write('{}\n'.format(act.rstrip()))
		elif act is filter_git_patterns.remove: pass
		else: raise ValueError(act)

filter_git_patterns.remove = object()

def cached_result(key_or_func=None, _key=None):
	if callable(key_or_func):
		_key = _key or key_or_func.func_name
		@ft.wraps(key_or_func)
		def _wrapper(self, *args, **kws):
			if _key not in self.c:
				self.c[_key] = key_or_func(self, *args, **kws)
			return self.c[_key]
		return _wrapper
	return ft.partial(cached_result, _key=key_or_func)


class GitWrapperError(Exception): pass

class GitWrapper(object):

	def __init__(self, conf, nacl):
		self.conf, self.nacl, self.c, self.lock = conf, nacl, dict(), None
		self.log = logging.getLogger('git')

	def __enter__(self):
		self.init()
		return self
	def __exit__(self, *err): self.destroy()
	def __del__(self): self.destroy()


	def init(self): pass # done lazily

	def init_lock(self, gitconfig):
		if self.lock: return
		lock = self.nacl.sha256(realpath(gitconfig), self.nacl.URLSafeBase64Encoder)[:8]
		lock = join(tempfile.gettempdir(), '.git-nerps.{}.lock'.format(lock))
		self.lock = open(lock, 'ab+')
		self.log.debug('Acquiring lock: %r', lock)
		fcntl.lockf(self.lock, fcntl.LOCK_EX)

	def destroy(self):
		if self.lock:
			self.log.debug('Releasing lock: %r', self.lock.name)
			self.lock.close()
			self.lock = None


	@property
	@cached_result
	def dev_null(self): return open(os.devnull, 'wb')

	run_error = subprocess.CalledProcessError

	def run(self, args=None, check=False, no_stderr=False, trap_code=None):
		kws = dict(close_fds=True)
		if no_stderr: kws['stderr'] = self.dev_null
		args = ['git'] + list(args or list())
		if self.log.isEnabledFor(logging.DEBUG):
			opts = ', '.join( bytes(k) for k, v in
				{'check': check, 'no-stderr': no_stderr, 'trap': trap_code}.items() if v )
			self.log.debug('run: %s [%s]', ' '.join(args), opts or '-')
		try: res = subprocess.check_output(args, **kws).splitlines()
		except self.run_error as err:
			if check: return False
			if trap_code:
				if trap_code is True: pass
				elif isinstance(trap_code, (int, long)): trap_code = [trap_code]
				if trap_code is True or err.returncode in trap_code: err = res = None
			if err: raise
		return res if not check else True

	def check(self, args=['rev-parse'], no_stderr=True):
		return self.run(args, check=True, no_stderr=no_stderr)

	def run_conf(self, args, gitconfig=None, **run_kws):
		gitconfig = gitconfig or self.path_conf
		return self.run(['config', '--file', gitconfig] + args, **run_kws)

	def sub(self, *path):
		if not self.c.get('git-dir'):
			p = self.run(['rev-parse', '--show-toplevel'])
			assert len(p) == 1, [p, 'rev-cache --show-toplevel result']
			self.c['git-dir'] = join(p[0], '.git')
		if not path: return self.c['git-dir']
		return join(self.c['git-dir'], *path)

	def param(self, *path):
		assert path and all(path), path
		return 'nerps.{}'.format('.'.join(path))

	@property
	@cached_result
	def path_conf_home(self):
		return expanduser(self.conf.git_conf_home)

	@property
	@cached_result
	def path_conf(self):
		is_git_repo = self.check()
		gitconfig = self.sub('config') if is_git_repo else self.path_conf_home
		self.path_conf_init(gitconfig, chmod_dir=is_git_repo)
		return gitconfig

	def path_conf_init(self, gitconfig, chmod_umask=None, chmod_dir=False):
		assert not self.lock
		self.init_lock(gitconfig)

		run_conf = ft.partial(self.run_conf, gitconfig=gitconfig)
		ver_k = self.param('version')
		ver = run_conf(['--get', ver_k], trap_code=1) or None
		if ver: ver = int(ver[0])
		if not ver or ver < self.conf.git_conf_version:

			if not ver:
				if not os.access(__file__, os.X_OK):
					self.log.warn( 'This script (%r) must be executable'
						' (e.g. run "chmod +x" on it) for git filters to work!' )
				script_link_abs = expanduser(self.conf.script_link)
				if not exists(script_link_abs) or not samefile(script_link_abs, __file__):
					try: os.unlink(script_link_abs)
					except (OSError, IOError): pass
					os.symlink(abspath(__file__), script_link_abs)

				run_conf(['--remove-section', 'filter.nerps'], trap_code=True)
				run_conf(['--remove-section', 'diff.nerps'], trap_code=True)

				script_cmd = ft.partial('{} {}'.format, self.conf.script_link)
				run_conf(['--add', 'filter.nerps.clean', script_cmd('git-clean')])
				run_conf(['--add', 'filter.nerps.smudge', script_cmd('git-smudge')])
				run_conf(['--add', 'diff.nerps.textconv', script_cmd('git-diff')])

				# See "Performing text diffs of binary files" in gitattributes(5)
				run_conf([ '--add', 'diff.nerps.cachetextconv', 'true'])

				# Placeholder item to work around long-standing bug with removing last value from a section
				# See: http://stackoverflow.com/questions/15935624/\
				#  how-do-i-avoid-empty-sections-when-removing-a-setting-from-git-config
				run_conf(['--add', self.param('n-e-r-p-s'), 'NERPS'])

			else: run_conf(['--unset-all', ver_k], trap_code=5)
			# Any future migrations go here
			run_conf(['--add', ver_k, bytes(self.conf.git_conf_version)])

		if chmod_umask is None: chmod_umask = self.conf.umask
		if chmod_dir:
			git_repo_dir = dirname(gitconfig)
			os.chmod(git_repo_dir, os.stat(git_repo_dir).st_mode & chmod_umask)
		os.chmod(gitconfig, os.stat(gitconfig).st_mode & chmod_umask)


	def _key_iter(self):
		key_re = re.compile(r'^{}\.(.*)$'.format(re.escape(self.param('key'))))
		for line in self.run_conf(['--list']):
			k, v = line.split('=', 1)
			m = key_re.search(k)
			if not m: continue
			yield m.group(1), v.strip()

	@property
	@cached_result
	def key_name_default(self):
		name = self.run_conf(['--get', self.param('key-default')], trap_code=1)
		name, = name or [None]
		return name

	@property
	@cached_result
	def key_name_any(self):
		try: k, v = next(self._key_iter())
		except StopIteration: return
		return k

	@property
	@cached_result
	def key_all(self):
		return list(self.nacl.key_decode(key, name) for name, key in self._key_iter())

	def key(self, name=None):
		name = name or self.key_name_default or self.key_name_any
		if not name:
			raise GitWrapperError('No keys found in config: {!r}'.format(self.path_conf))
		key = self.run_conf(['--get', self.param('key', name)])
		if not key:
			raise GitWrapperError(( 'Key {!r} is set as default'
				' but is unavailable (in config: {!r})' ).format(name, self.path_conf))
		key, = key
		self.log.debug('Using key: %s', name)
		return self.nacl.key_decode(key, name)



def is_encrypted(conf, src_or_line, rewind=True):
	if not isinstance(src_or_line, types.StringTypes):
		pos = src_or_line.tell()
		line = src_or_line.readline()
		src_or_line.seek(pos)
		src_or_line = line
	nerps, ver = src_or_line.strip().split(None, 2)[:2]
	return nerps == conf.enc_watermark

def encrypt(conf, nacl, git, log, name, src=None, dst=None):
	key = git.key(name)
	plaintext = src.read()
	nonce = conf.nonce_func(plaintext)
	ciphertext = key.encrypt(plaintext, nonce)
	dst_stream = io.BytesIO() if not dst else dst
	dst_stream.write('{} {}\n\n'.format(conf.enc_watermark, conf.git_conf_version))
	dst_stream.write(ciphertext.encode('base64'))
	if not dst: return dst_stream.getvalue()

def decrypt(conf, nacl, git, log, name, src=None, dst=None, strict=False):
	key = git.key(name)
	header = src.readline()
	nerps, ver = header.strip().split(None, 2)[:2]
	assert nerps == conf.enc_watermark, nerps
	assert int(ver) <= conf.git_conf_version, ver
	ciphertext = src.read().strip().decode('base64')
	try: plaintext = key.decrypt(ciphertext)
	except nacl.CryptoError:
		if strict: raise
		err_t, err, err_tb = sys.exc_info()
		log.debug( 'Failed to decrypt with %s key %r: %s',
			'default' if not name else 'specified', key.name, err )
		for key_chk in git.key_all:
			if key_chk.name == key.name: continue
			log.debug('Trying key: %s', key_chk.name)
			try: plaintext = key_chk.decrypt(ciphertext)
			except nacl.CryptoError: pass
			else: break
		else: raise err_t, err, err_tb
	if dst: dst.write(plaintext)
	else: return plaintext



def run_command(opts, conf, nacl, git):
	log = logging.getLogger(opts.cmd)
	exit_code = 0

	##########
	if opts.cmd == 'key-gen':
		key_raw = nacl.random(nacl.SecretBox.KEY_SIZE)
		key = nacl.key_decode(key_raw, raw=True)
		key_str = nacl.key_encode(key)

		if opts.print or opts.verbose:
			print('Key:\n  ', key_str, '\n')
			if opts.print: return

		gitconfig = git.path_conf
		run_conf = ft.partial(git.run_conf, gitconfig=gitconfig)

		name = opts.name
		if not name:
			for name in conf.key_name_pool:
				k = git.param('key', name)
				if not run_conf(['--get', k], check=True, no_stderr=True): break
			else:
				raise opts.parser.error('Failed to find unused'
					' key name, specify one explicitly with --name.')
		k = git.param('key', name)

		log.info('Adding key %r to gitconfig (k: %s): %r', name, k, gitconfig)

		# To avoid flashing key on command line (which can be seen by any
		#  user in same pid ns), "git config --add" is used with unique tmp_token
		#  here, which is then replaced (in the config file) by actual key.

		with open(gitconfig, 'rb') as src: gitconfig_str = src.read()
		while True:
			tmp_token = os.urandom(18).encode('base64').strip()
			if tmp_token not in gitconfig_str: break

		commit = False
		try:
			run_conf(['--add', k, tmp_token])
			with edit(gitconfig) as (src, tmp):
				gitconfig_str = src.read()
				assert tmp_token in gitconfig_str, tmp_token
				tmp.write(gitconfig_str.replace(tmp_token, key_str))
			commit = True
		finally:
			if not commit: run_conf(['--unset', k])

		if opts.set_as_default:
			k = git.param('key-default')
			run_conf(['--unset-all', k], trap_code=5)
			run_conf(['--add', k, name])


	##########
	elif opts.cmd == 'key-set':
		if opts.name_arg: opts.name = opts.name_arg

		k_dst = git.param('key-default')

		k = git.run_conf(['--get', k_dst])

		if k: # make sure default key is the right one and is available
			k, = k
			k_updated = opts.name and k != opts.name and opts.name
			if k_updated: k = opts.name
			v = git.run_conf(['--get', git.param('key', k)])
			if not v and opts.name:
				opts.parser.error('Key %r was not found in config file: %r', k, git.path_conf)
			k = None if not v else (k_updated or True) # True - already setup

		if not k: k = git.key_name_any # pick first random key

		if k and k is not True:
			git.run_conf(['--unset-all', k_dst], trap_code=5)
			git.run_conf(['--add', k_dst, k])


	##########
	elif opts.cmd == 'git-clean':
		encrypt(conf, nacl, git, log, opts.name, src=sys.stdin, dst=sys.stdout)
		sys.stdout.close() # to make sure no garbage data will end up there

	##########
	elif opts.cmd == 'git-smudge':
		decrypt( conf, nacl, git, log, opts.name,
			src=sys.stdin, dst=sys.stdout, strict=opts.name_strict )
		sys.stdout.close() # to make sure no garbage data will end up there

	##########
	elif opts.cmd == 'git-diff':
		decrypt( conf, nacl, git, log, opts.name,
			src=open(opts.path, 'rb'), dst=sys.stdout, strict=opts.name_strict )
		sys.stdout.close() # to make sure no garbage data will end up there


	##########
	elif opts.cmd == 'encrypt':
		if opts.path:
			with edit(opts.path) as (src, tmp):
				if not opts.force and is_encrypted(conf, src): raise edit.cancel
				encrypt(conf, nacl, git, log, opts.name, src=src, dst=tmp)
		else: encrypt(conf, nacl, git, log, opts.name, src=sys.stdin, dst=sys.stdout)

	##########
	elif opts.cmd == 'decrypt':
		if opts.path:
			with edit(opts.path) as (src, tmp):
				if not opts.force and not is_encrypted(conf, src): raise edit.cancel
				decrypt( conf, nacl, git, log, opts.name,
					src=src, dst=tmp, strict=opts.name_strict )
		else:
			decrypt( conf, nacl, git, log, opts.name,
				src=sys.stdin, dst=sys.stdout, strict=opts.name_strict )


	##########
	elif opts.cmd in ('taint', 'untaint'):
		if not git.check(): opts.parser.error('Can only be run inside git repository')

		for path in opts.path:
			path_rel = relpath(path, git.sub('..'))
			assert not re.search(r'^(\.|/)', path_rel), path_rel
			attrs_file = normpath(git.sub(
				'../.gitattributes' if not opts.local_only else 'info/attributes' ))

			with edit(attrs_file) as (src, tmp):
				n, matches_mark, matches = None, dict(), filter_git_patterns(src, tmp, path_rel)
				while True:
					try: n, line, pat, filters = next(matches) if n is None else matches.send(act)
					except StopIteration: break
					act = None
					if opts.cmd == 'taint':
						if not opts.force:
							if not opts.silent:
								log.error( 'gitattributes (%r) already has matching'
									' pattern for path %r, not adding another one (line %s): %r',
									attrs_file, path_rel, n, line )
								# XXX: check if that line also has matching filter, add one
								exit_code = 1
							raise edit.cancel
					if opts.cmd == 'untaint':
						# XXX: check if line has actually **matching** filter
						matches_mark[n] = line

				if opts.cmd == 'taint':
					tmp.write('/{} filter=nerps diff=nerps\n'.format(path_escape(path_rel)))

				if opts.cmd == 'untaint':
					if not matches_mark:
						if not opts.silent:
							log.error( 'gitattributes (%r) pattern'
								' for path %r was not found', attrs_file, path_rel )
							exit_code = 1
						raise edit.cancel
					if not opts.force and len(matches_mark) > 1:
						log.error( 'More than one gitattributes (%r) pattern was'
							' found for path %r, aborting: %r', attrs_file, path_rel, matches_mark.values() )
						exit_code = 1
						raise edit.cancel
					src.seek(0)
					tmp.seek(0)
					tmp.truncate()
					for n, line in enumerate(iter(src.readline, ''), 1):
						if n not in matches_mark: tmp.write(line)


	else: opts.parser.error('Unrecognized command: {}'.format(opts.cmd))
	return exit_code


def main(args=None, defaults=None):
	nacl, args = NaCl(), sys.argv[1:] if args is None else args
	conf = defaults or Conf(nacl)

	import argparse
	parser = argparse.ArgumentParser(description='Tool to manage encrypted files in a git repo.')

	parser.add_argument('-d', '--debug', action='store_true', help='Verbose operation mode.')

	parser.add_argument('-n', '--name',
		help='Key name to use.'
			' Can be important or required for some commands (e.g. "key-set").'
			' For most commands, default key gets'
				' picked either as a first one or the one explicitly set as such.'
			' When generating new key, default is to pick some'
				' unused name from the phonetic alphabet letters.')

	parser.add_argument('-s', '--name-strict',
		help='Only try specified or default key for decryption.'
			' Default it to try other ones if that one fails, to see if any of them work for a file.')

	cmds = parser.add_subparsers(
		dest='cmd', title='Actions',
		description='Supported actions (have their own suboptions as well)')


	cmd = 'Generate new encryption key and store or just print it.'
	cmd = cmds.add_parser('key-gen', help=cmd, description=cmd,
		epilog='Default is to store key in a git repository config'
				' (but dont set it as default if there are other ones already),'
				' if inside git repo, otherwise store in the home dir'
				' (also making it default only if there was none before it).'
			' Use "key-set" command to pick default key for git repo, user or file.'
			' System-wide and per-user gitconfig files are never used for key storage,'
				' as these are considered to be a bad place to store anything private.')

	cmd.add_argument('-p', '--print', action='store_true',
		help='Only print the generated key, do not store anywhere.')
	cmd.add_argument('-v', '--verbose', action='store_true',
		help='Print generated key in addition to storing it.')

	cmd.add_argument('-g', '--git', action='store_true',
		help='Store new key in git-config.')
	cmd.add_argument('-d', '--homedir', action='store_true',
		help='Store new key in the {} file (in user home directory).'.format(conf.git_conf_home))

	cmd.add_argument('-s', '--set-as-default', action='store_true',
		help='Set generated key as default in whichever config it will be stored.')

	# XXX: option to generate from ssh private key


	cmd = 'Set default encryption key for a repo/homedir config.'
	cmd = cmds.add_parser('key-set', help=cmd, description=cmd,
		epilog='Same try-repo-then-home config order as with key-gen command.'
			' Key name should be specified with the --name option.'
			' If no --name will be specified and there is no default key set'
				' or it no longer available, first (any) available key will be set as default.')
	cmd.add_argument('name_arg', nargs='?',
		help='Same as using global --name option, but overrides it if both are used.')


	cmd = 'Encrypt file before comitting it into git repository - "clean" from secrets.'
	cmd = cmds.add_parser('git-clean', help=cmd, description=cmd,
		epilog='Intended to be only used by git, use "encrypt" command from terminal instead.')
	cmd.add_argument('path', help='Filename suppled by git.'
		' Not used, since git supplies file contents'
			' to stdin and expects processing results from stdout.')

	cmd = 'Decrypt file when getting it from git repository - "smudge" it with secrets.'
	cmd = cmds.add_parser('git-smudge', help=cmd, description=cmd,
		epilog='Intended to be only used by git, use "decrypt" command from terminal instead.')
	cmd.add_argument('path', help='Filename suppled by git.'
		' Not used, since git supplies file contents'
			' to stdin and expects processing results from stdout.')

	cmd = 'Decrypt file when getting it from git repository for diff generation purposes.'
	cmd = cmds.add_parser('git-diff', help=cmd, description=cmd,
		epilog='Intended to be only used by git, use "decrypt" command from terminal instead.')
	cmd.add_argument('path', help='Filename suppled by git.')


	cmd = 'Encrypt file in-place or process stdin to stdout.'
	cmd = cmds.add_parser('encrypt', help=cmd, description=cmd)
	cmd.add_argument('path', help='Path to a file to encrypt.'
		' If not specified, stdin/stdout streams will be used instead.')
	cmd.add_argument('-f', '--force', action='store_true',
		help='Encrypt even if file appears to be encrypted already.')

	cmd = 'Decrypt file in-place or process stdin to stdout.'
	cmd = cmds.add_parser('decrypt', help=cmd, description=cmd)
	cmd.add_argument('path', help='Path to a file to decrypt.'
		' If not specified, stdin/stdout streams will be used instead.')
	cmd.add_argument('-f', '--force', action='store_true',
		help='Decrypt even if file does not appear to be encrypted.')


	cmd = 'Mark file(s) to be transparently encrypted in the current git repo.'
	cmd = cmds.add_parser('taint', help=cmd, description=cmd,
		epilog='Adds files to .gitattributes (default)'
				' or .git/info/attributes (see --local-only option).')

	cmd.add_argument('path', nargs='+', help='Path of a file to mark.')

	cmd.add_argument('-f', '--force', action='store_true',
		help='Add pattern to gitattributes even if'
			' there are matching ones already, skip extra checks.')
	cmd.add_argument('-s', '--silent', action='store_true',
		help='Do not print any errors if file is already marked.')

	cmd.add_argument('-l', '--local-only', action='store_true',
		help='Add file to .git/info/attributes (which'
			' does not usually get shared) instead of .gitattributes.')


	cmd = 'Remove transparent encryption mark from a file(s).'
	cmd = cmds.add_parser('untaint', help=cmd, description=cmd,
		epilog='Removes file(s) from .gitattributes (default)'
				' or .git/info/attributes (see --local-only option).')

	cmd.add_argument('path', nargs='+', help='Path of a file to unmark.')

	cmd.add_argument('-f', '--force', action='store_true',
		help='Remove any number of any matching patterns from gitattributes, skip extra checks.')
	cmd.add_argument('-s', '--silent', action='store_true',
		help='Do not print any errors if file does not seem to be marked.')

	cmd.add_argument('-l', '--local-only', action='store_true',
		help='Remove pattern from .git/info/attributes (which'
			' does not usually get shared) instead of .gitattributes.')


	opts = parser.parse_args(args)

	logging.basicConfig(level=logging.DEBUG if opts.debug else logging.WARNING)
	log = logging.getLogger('main')

	# To avoid influence from any of the system-wide aliases
	os.environ['GIT_CONFIG_NOSYSTEM'] = 'true'

	with GitWrapper(conf, nacl) as git:
		opts.parser = parser
		return run_command(opts, conf, nacl, git)


if __name__ == '__main__': sys.exit(main())
