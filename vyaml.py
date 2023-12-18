#!/usr/bin/env python3

import argparse
import io
import os
import re
import subprocess
import sys
import textwrap
from binascii import hexlify, unhexlify
from typing import Any

import yaml
from Crypto.Cipher import AES
from Crypto.Protocol.KDF import scrypt
from Crypto.Random import get_random_bytes

# use vyos library from system path
sys.path.append('/usr/lib/python3/dist-packages')
from vyos.config import Config

IMAGE_LINE_RE = re.compile(r'^set container name (.*) image (.*)$')


class VYaml:
    key: str

    def __init__(self) -> None:
        parser = argparse.ArgumentParser(prog='vyaml', description='Toolkit to use configuration written in YAML for VyOS routers.')
        parser.add_argument('-v', '--version', action='version', version='%(prog)s __version__')
        commands = parser.add_subparsers(required=True, title='commands')

        encrypt_cmd = commands.add_parser('encrypt', description='Encrypts input on stdin using AES-256-GCM with scrypt.')
        encrypt_cmd.set_defaults(func=self.encrypt_cmd)
        encrypt_cmd.add_argument('-k', '--key', type=argparse.FileType('r'), metavar='PATH', help='path to encryption key', required=True)

        render_cmd = commands.add_parser('render', description='Outputs VyOS commands based on the config.')
        render_cmd.set_defaults(func=self.render_cmd)
        render_cmd.add_argument('-k', '--key', type=argparse.FileType('r'), metavar='PATH', help='path to encryption key')
        render_cmd.add_argument('-c', '--config', type=argparse.FileType('r'), metavar='PATH', help='path to config file', required=True)

        apply_cmd = commands.add_parser('apply', description='Applies the config, then commits and saves it.\n'
                                                             'Any container images missing or updated with be pulled, and containers with updated images will be restarted.')
        apply_cmd.set_defaults(func=self.apply_cmd)
        apply_cmd.add_argument('-k', '--key', type=argparse.FileType('r'), metavar='PATH', help='path to encryption key')
        apply_cmd.add_argument('-c', '--config', type=argparse.FileType('r'), metavar='PATH', help='path to config file', required=True)

        import_cmd = commands.add_parser('import', description='Converts running config to YAML.')
        import_cmd.set_defaults(func=self.import_cmd)

        args = parser.parse_args(sys.argv[1:])
        args.func(args)

    def encrypt_cmd(self, args: argparse.Namespace) -> None:
        self.load_key(args.key)

        if sys.stdin.isatty():
            print('Reading plaintext input from stdin '
                  '(ctrl+d to end input, thrice if the '
                  'input does not have a newline)', file=sys.stderr)

        if len(secret := '\n'.join(sys.stdin.readlines()).strip()) > 0:
            ciphertext = self.encrypt(secret, self.key)

            # output encrypted data pre-formatted for yaml
            print(' !secret |')
            print(textwrap.indent(textwrap.fill(ciphertext, width=64), ' ' * 8))
        else:
            self.error('stdin was empty')

    def render_cmd(self, args: argparse.Namespace) -> None:
        self.load_key(args.key)

        config = self.load_config(args.config)
        for line in self.flatten_config(config):
            print(line)

    def apply_cmd(self, args: argparse.Namespace) -> None:
        self.load_key(args.key)
        config = self.load_config(args.config)
        images = self.image_list()
        containers: list[str] = []

        # start from a clean slate
        lines = [
            'configure',
            'load /opt/vyatta/etc/config.boot.default >/dev/null',
            'delete service ntp',  # because it comes with too open defaults
        ] + self.flatten_config(config)

        # iterate through lines and find container images
        for line in lines.copy():
            if match := IMAGE_LINE_RE.search(line):
                if (image := match.group(2)) not in images:
                    # new image, we must pull it
                    lines.append(f'run add container image {image}')
                    # and mark it for restart
                    containers.append(match.group(1))

        # output changes before they are committed and saved
        lines.append('show | compare')
        lines.append('commit')
        lines.append('save')

        # lastly we'll want to restart containers with new images
        for container in containers:
            lines.append(f'run restart container {container}')

        print(self.execute_vbash(lines))

    def import_cmd(self, _args: argparse.Namespace) -> None:
        # grab running config
        vc = Config()
        config = vc.get_config_dict().copy()  # need to copy it to avoid metadata

        # output the config as yaml
        print(yaml.safe_dump(config))

    def load_key(self, key_file: io.TextIOWrapper) -> None:
        if key_file and len(key := key_file.readline().strip()) > 0:
            self.key = key

    def load_config(self, config_file: io.TextIOWrapper) -> Any:
        yaml.SafeLoader.add_constructor('!secret', self.secret_tag_constructor)
        yaml.SafeLoader.add_constructor('!env', self.env_tag_constructor)
        yaml.SafeLoader.add_constructor('!include', self.include_tag_constructor)

        try:
            config = yaml.safe_load(config_file)
        except yaml.scanner.ScannerError as e:
            self.error(str(e))
        except yaml.composer.ComposerError as e:
            self.error(str(e))
        except UnicodeDecodeError:
            self.error('config file is not a text file')
        return config

    def secret_tag_constructor(self, _loader: yaml.SafeLoader, node: yaml.nodes.ScalarNode) -> str:
        try:
            return self.decrypt(node.value.replace('\n', '').strip(), self.key)
        except AttributeError:
            self.error(f'unable to decrypt; missing encryption key\n{node.start_mark}')
        return ''  # too harsh type checks

    def env_tag_constructor(self, _loader: yaml.SafeLoader, node: yaml.nodes.ScalarNode) -> str:
        return os.environ.get(node.value) or ''

    def include_tag_constructor(self, loader: yaml.SafeLoader, node: yaml.nodes.ScalarNode) -> Any:
        if node.value.startswith(os.sep):  # absolute path
            path = node.value
        else:  # relative path
            path = os.path.join(os.path.dirname(loader.name), node.value)

        if not os.path.isfile(path):
            self.error(f'file "{path}" not found\n{str(node.start_mark)}')
        if not os.access(path, os.R_OK):
            self.error(f'permission denied when reading file "{path}"\n{node.start_mark}')

        with open(path, 'r', encoding='utf-8') as include_file:
            return yaml.safe_load(include_file)

    def flatten_config(self, config: dict[Any, Any]) -> list[str]:
        lines: list[str] = []
        self.flatten_config_obj(lines, config)
        return lines

    def flatten_config_obj(self, lines: list[str], obj: Any = '', prefix: str = '') -> None:
        if isinstance(obj, dict) and obj:
            for key in obj:
                self.flatten_config_obj(lines, obj[key], prefix + str(key) + ' ')
        elif isinstance(obj, list) and obj:
            for key in obj:
                self.flatten_config_obj(lines, key, prefix)
        elif not prefix.startswith('.'):
            if obj:
                lines.append(f"set {prefix[:-1]} '{str(obj)}'")
            else:
                lines.append(f"set {prefix[:-1]}")

    def encrypt(self, plaintext: str, passphrase: str) -> str:
        salt = get_random_bytes(32)
        key = scrypt(passphrase, str(salt), 32, N=2**16, r=8, p=1)
        cipher = AES.new(key, AES.MODE_GCM)  # type: ignore
        ciphertext, tag = cipher.encrypt_and_digest(plaintext.encode('utf8'))  # type: ignore
        return hexlify(b'\n\r'.join([ciphertext, salt, cipher.nonce, tag])).decode()  # type: ignore

    def decrypt(self, ciphertext: bytes, passphrase: str) -> str:
        try:
            ciphertext, salt, nonce, tag = unhexlify(ciphertext).split(b'\n\r')
            key = scrypt(passphrase, str(salt), 32, N=2**16, r=8, p=1)
            cipher = AES.new(key, AES.MODE_GCM, nonce)  # type: ignore
            return cipher.decrypt_and_verify(ciphertext, tag).decode()  # type: ignore
        except ValueError as e:
            if str(e) == 'MAC check failed':
                # better understandable error message
                self.error('unable to decrypt; incorrect encryption key')
        return ''  # too harsh type checks

    def execute_vbash(self, lines: list[str] | str) -> str:
        if not isinstance(lines, list):
            lines = [lines]

        # inject vyos env
        lines.insert(0, 'source /opt/vyatta/etc/functions/script-template')

        try:
            return subprocess.check_output('/bin/vbash', input='\n'.join(lines).encode('utf8')).decode()
        except subprocess.CalledProcessError:
            # vbash throws errors directly to stderr
            sys.exit(1)

    def image_list(self) -> list[str]:
        output: str = self.execute_vbash('run show container image')
        images: list[str] = []

        for line in output.strip().split('\n')[1:]:
            images.append(':'.join(line.split()[:2]))
        return images

    def error(self, msg: str) -> None:
        print(f'error: {msg}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    VYaml()
