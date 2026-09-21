# Local Zulip test server

This fixture runs the official Zulip 12.2 image in a dedicated Colima VM on this Mac. Only `https://zulip.localhost:8443` is published. Database/cache services are not exposed to the network. It uses generated local secrets, a local HTTPS certificate, console-only email, and no community accounts.

For connecting Slack and a real Zulip community, follow [INSTALL.md](../INSTALL.md).

## Start or resume

From the project directory:

```sh
colima start zulip-bridge --activate=false --ssh-config=false
./local/control up -d zulip --wait
```

Open **https://zulip.localhost:8443**. The browser will warn about the local certificate because it is deliberately not installed in your system trust store. Accept that certificate only for this local test site. The bridge uses the supplied certificate directly and keeps certificate verification enabled.

Login credentials are saved in `local/credentials.json`, which is private and excluded from Git. It includes the test administrator, Alice, Bob, and the Generic bot’s API credentials. The humans’ login emails are `admin@bridge.test`, `alice@bridge.test`, and `bob@bridge.test`. The bot is owned by ordinary member Alice, not the administrator. The shared test channel is `bridge-test`.

## Connect the local server to real Slack

After provisioning and the smoke test, prepare the local bridge files:

```sh
python3 local/configure.py
```

Follow the Slack steps in [INSTALL.md](../INSTALL.md). Put the Slack workspace/channel IDs into `local/bridge.local.toml` and its two tokens into `local/.env.local`. The Zulip values and certificate path are filled in automatically. Then:

```sh
uv --no-config run --env-file local/.env.local chat-bridge --config local/bridge.local.toml check
uv --no-config run --env-file local/.env.local chat-bridge --config local/bridge.local.toml run
```

## Stop

```sh
./local/control stop
colima stop zulip-bridge
```

This frees the VM’s memory and preserves the test accounts/messages. Nothing is configured to start at login. Do not use `down --volumes` unless you intentionally want to erase this disposable fixture.

## Repeat the API smoke test

```sh
uv --no-config run python local/smoke.py
```

It exercises the real bridge engine and real Zulip server, with **simulated Slack**. It tests posting, editing, reactions, thread promotion, native notice loop prevention, queue correlation echoes, reverse events and deletion. It creates synthetic messages in `bridge-test` and backdates only one of those messages by 15 minutes to verify late-edit/delete notices under unchanged policies. Results are written to `local/test-report.json`; no credentials are included in the report. Connecting real Slack still requires its two tokens and channel/workspace IDs.

## Rebuild on another Mac

Requires Homebrew, Python 3.12+, uv, and enough free disk for Zulip and its supporting services. Allow roughly 10–15 GB of working room; image-transfer archives need temporary extra space. Run from the project directory:

```sh
brew install colima docker docker-compose
colima start zulip-bridge --cpu 2 --memory 4 --disk 12 --root-disk 8 \
  --vm-type vz --activate=false --ssh-config=false \
  --mount "$PWD/local:w"
python3 local/prepare.py
./local/control pull
./local/control run --rm zulip app:init
./local/control up -d zulip --wait
```

`prepare.py` pins the official Compose repository revision, generates credentials only if absent, and configures local TLS. It uses OpenSSL with `-addext` support; `brew install openssl@3` supplies that if needed. `control` always targets the dedicated VM and a private Docker client configuration, independent of the active Docker context.

On this Mac, the VM could not reach image registries. The host-side fallback is:

```sh
python3 local/load-images.py
```

This uses curl to download official Linux arm64 images, verifies downloaded layer digests, and loads them into the VM one at a time. It records image digests in `local/image-digests.json`. It is specific to this Apple Silicon fixture; normally use `./local/control pull`.

Provision disposable local accounts, then keep the generated credential file private:

```sh
umask 077
./local/control exec -T -u zulip zulip \
  /home/zulip/deployments/current/manage.py shell < local/provision_server.py
./local/control cp zulip:/tmp/bridge-credentials.json local/credentials.json
chmod 600 local/credentials.json
uv --no-config run python local/smoke.py
```

The provisioning script is intentionally tied to the pinned Zulip server version and refuses an unrelated organization. It is fixture administration, not code the remote Zulip community needs to run. Save the local credential file before recreating containers; `/tmp` inside the container is not a persistent credential backup. If you need to rerun provisioning after recreating a container, first restore it with `./local/control cp local/credentials.json zulip:/tmp/bridge-credentials.json` and make it readable only by the container’s `zulip` user. Normal stop/start does not need provisioning.

Official reference: [Zulip Docker setup](https://zulip.readthedocs.io/projects/docker/en/latest/how-to/compose-getting-started.html).
