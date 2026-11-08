# romm-shelf

A controller-driven [RomM](https://romm.app) client for [RetroDECK](https://retrodeck.net):

- browse your RomM library like an app store
- download games straight into RetroDECK's ROM folders
- launch a game as its own Steam app, controllers from Steam Input
- sync saves and states with RomM on game start, game exit, and shutdown

**Status:** design. See [docs/design.md](docs/design.md) for the design and [todo.md](todo.md) for open decisions. Windows games are packed as one squashfs and run through Proton; see [docs/winapps.md](docs/winapps.md) for building one. The on-device folders and the one-time BIOS, firmware and Switch-key import are in [docs/bios.md](docs/bios.md). To try it on Windows, [docs/wsl.md](docs/wsl.md) has the WSL2 installer (a batch file, with GPU and controller caveats).

**Governing rule:** RetroDECK's folders and the RomM server are the only sources of truth. romm-shelf keeps no state of its own that can't be rebuilt from them, so uninstalling it leaves an ordinary RetroDECK install.

Requires RomM 5.4 or later, which added the device sync protocol fields romm-shelf uses.

## Patches: customise anything

Every change romm-shelf makes to the system is a **patch**: an ordinary Ansible playbook. Patches are how you customise things in depth: an emulator's settings, a shader for one game, a fix that only one game needs. The workflow we want to support is asking an AI agent for the change you want ("give Contra a curved CRT look", "map Mesen's rewind to L2"). The agent writes the playbook, you read its plan, and you apply it.

- **A patch is one `NAME.yml`.** Every Ansible module works (`lineinfile`, `ini_file`, `copy`, `template`, …). It can declare settings under `x_settings`, and romm-shelf draws those as a form in the UI. It can be scoped to every emulator (`x_scope: emulator`) or to one game at a time (`x_scope: game`). The bundled ones in [src/romm_shelf/playbooks/](src/romm_shelf/playbooks/) are working examples to start from.
- **See before it changes anything.** `romm-shelf patches apply NAME --plan` runs the playbook in check mode and prints every change as a diff. Apply it from the Patches tab or with `romm-shelf patches apply NAME`. Nothing records what was applied: a patch is *applied* when its plan is empty.
- **Where patches live**, a later layer replacing a patch with the same name:
  1. built into romm-shelf
  2. **RomM firmware** named `*.yml`, on any platform: listed on the Patches tab of every device
  3. **a game's own**, in a `patch/` folder of its RomM folder ROM: listed only in that game's settings
  4. **yours**, in `~/.config/romm-shelf/patches/` on one device

  To try a patch out, put it in your own folder. To give it to every device, put it in RomM.

```yaml
# roms/nes/Contra (USA)/patch/contra-shader.yml, in RomM
- name: Contra's shader
  hosts: all
  gather_facts: false
  vars:
    x_scope: game
    x_settings:
      curved: {title: Curved screen, default: true}
  tasks:
    - name: …
```

Patches from RomM run code on every paired device, so only put patches you trust on the server. For now that means your own and your agent's. Community patches may come later. See [docs/design.md § Patches](docs/design.md#patches) for the details.
