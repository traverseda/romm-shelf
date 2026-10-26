# romm-shelf

A controller-driven [RomM](https://romm.app) client for [RetroDECK](https://retrodeck.net):

- browse your RomM library like an app store
- download games straight into RetroDECK's ROM folders
- sync saves and states with RomM on game start, game exit, and shutdown

**Status:** design. See [docs/design.md](docs/design.md) for the design and [todo.md](todo.md) for open decisions.

**Governing rule:** RetroDECK's folders and the RomM server are the only sources of truth. romm-shelf keeps no state of its own that can't be rebuilt from them, so uninstalling it leaves an ordinary RetroDECK install.

Requires RomM 5.4 or later, which added the device sync protocol fields romm-shelf uses.
