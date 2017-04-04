#! /usr/bin/env python
# -*- coding: utf-8 -*

VERSION='0.1.5'
APPNAME='rkflashkit'


top = '.'
out = 'build'
src = 'src'
install_path = 'debian/usr/share/rkflashkit/lib'


def options(opt):
  opt.load('python')


def configure(conf):
  conf.load('python')
  conf.check_python_version((2,6,0))


def build(bld):
  bld(features='py',
      source=bld.path.ant_glob(src + '/**/*.py'),
      install_from=src,
      install_path=install_path)
  if bld.cmd == 'install':
    start_dir = bld.path.find_dir('src')
    bld.install_files(install_path,
        bld.path.ant_glob(src + '/rkflashkit/**/*'),
        cwd=start_dir, relative_trick=True)


def chmod(ctx):
  print('Creating debian package ...')
  ctx.exec_command('chmod -R a+rX debian')


def build_debian(ctx):
  print('Creating debian package ...')
  ctx.exec_command('fakeroot dpkg -b debian .')


def debian(ctx):
  from waflib import Options
  commands = ['configure', 'build', 'install', 'chmod', 'build_debian']
  Options.commands = commands + Options.commands

