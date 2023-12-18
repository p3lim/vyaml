# vyaml

A commandline toolkit for configuring [VyOS](https://vyos.io) devices in [YAML](https://yaml.org).  
The YAML configuration can be enhanced with [custom tags](#tags), and applied directly.

### Commands

`vyaml apply` applies a configuration in the following fashion:

- loads default configuration
- applies the configuration from YAML, decrypting any secrets in runtime
- displays the difference (equivalent to `show | compare`)
- commits and saves
- if there were containers configured it will also:
	- pull new images
	- restart containers with updated images

`vyaml render` will convert the configuration to `set` commands and output then to the screen without running them.

`vyaml encrypt` will read input on stdin and encrypt it so it can be added as a secret to the configuration.

`vyaml import` will convert the running config to YAML and output it to stdout.

### Tags

Additional [YAML tags](https://github.com/yaml/yaml-spec/blob/main/spec/1.2.2/spec.md#-tags) supported:

- `!env` will replace the value with an environment variable (e.g. `!env SHELL` becomes `/bin/vbash`)
- `!secret` will replace the value with an [encrypted secret](#secrets)

### Secrets

Secrets generated with `vyaml encrypt` are encrypted with [AES-256-GCM](https://en.wikipedia.org/wiki/Galois/Counter_Mode), with a key derivated with [scrypt](https://en.wikipedia.org/wiki/Scrypt), and should be safe to commit to Git.

To use secrets in a configuration, or to encrypt data, a key file must be supplied using the `-k`/`--key` argument.

### Examples

```yaml
system:
  host-name: vyos
  name-server:
    - 1.1.1.1
    - 1.0.0.1

  login:
    user:
      !env USER:
        authentication:
          plaintext-password: !secret |
            656a34220330e6659cc40b0a0dafcb9cf04efcda530c170722da9b8a318c7584
            a9811da7eda054a845c8f2e1410a0dcf034f6ad37207e0da1a819d31d6ef650a
            0da3cf0186e35f688db1548038695c5e6f

interface:
  ethernet:
    eth0:
      address: dhcp

service:
  ssh:
    port: 22

container:
  network:
    nginx:
      prefix: 172.20.0.0/16

  name:
    nginx:
      image: nginx:latest
      network: nginx
      port:
        http:
          source: 80
          destination: 80
```

The above configuration becomes:

```bash
set system host-name vyos
set system name-server 1.1.1.1
set system name-server 1.0.0.1
set system login user vyos authentication plaintext-password supersecret
set interface ethernet eth0 address dhcp
set service ssh port 22
set container network nginx prefix 172.20.0.0/16
set container name nginx image nginx:latest
set container name nginx network nginx
set container name nginx port http source 80
set container name nginx port http destination 80
```

You can also shorten down some of the nesting:
```yaml
container:
  name:
    nginx:
      port:
        http:
          source: 80
          destination: 80
```
is equivalent to:
```yaml
container name:
  nginx:
    port http source: 80
    port http destination: 80
```
or even this, although then it's practically like running commands directly:
```yaml
container name nginx port http source: 80
container name nginx port http destination: 80
```

## Installation

Releases are available here on GitHub, packaged into one executable without any external Python dependencies.

<https://github.com/p3lim/vyaml/releases>

Add this to `/config/scripts/` (so it survives reboots/upgrades) and make it executable.  
To add it to the path, run the following command:

```bash
echo 'sudo ln -sf /config/scripts/vyaml /usr/local/bin/vyaml' | sudo tee -a /config/scripts/vyos-postconfig-bootup.script
sh /config/scripts/vyos-postconfig-bootup.script
```
