# Run state

`runs/<run_id>/state.json` records every step of a run: `pending`, `running` (claimed by a
session, with an expiry), `done`, `failed` or `interrupted`. Every Colab session reads it after
pulling and skips finished or claimed steps. Written by `uncertainscene.infra.state`; do not edit
by hand unless resetting a step on purpose (then commit as `infra(colab): reset <run>/<step>`).
