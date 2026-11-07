# Security

Please report a vulnerability through
[GitHub's private reporting](https://github.com/Marc-Dvci/Patchbay/security/advisories/new), not in a public issue.
The supported version is the `main` branch.

Patchbay has no hosted service. What it exposes on the machine that runs it:

- the drivers `i2c-virtio` and `spi-virtio` inside a QNX guest, which parse what the virtio device returns;
- the back end's vhost-user and control sockets, Unix sockets in the user's own working directory;
- the patch panel, an HTTP server bound to 127.0.0.1 only, which validates every fault request before passing it on.

The reports that matter most: a device answer that makes a driver crash, hang or write outside its buffers, and a
request to the back end or the panel that reaches memory or files it should not.
