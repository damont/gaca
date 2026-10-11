"""`gaca-api install`: set a GACA server up on this machine.

Run through the repo's `install.sh`, which only gets `uv` and the dependencies in place and
then hands over to this package. Everything here is a small, idempotent step so the installer
can be re-run after an interruption or an update (it keeps your secrets and data, rewrites
the service, and carries on where it left off).
"""
