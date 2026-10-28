Wallet funds and privacy profile: apply this only to changed wallet funds,
privacy, signing, descriptors, key handling, coin selection, fee and change
logic, backup and restore, rescans, and wallet persistence. It refines the
adversarial review; do not treat it as a separate stage.

Start from the wallet operation and its authorization contract. Identify what
the user or RPC caller requests, which keys and coins the wallet controls, and
which transaction, PSBT, descriptor, or prevout data comes from elsewhere. Check
the selected script version and sighash commitments before claiming a signature
authorizes different inputs, recipients, amounts, or fees. Do not assume a
confirmation UI or an all-outputs commitment that the workflow does not promise.

Trace construction, funding, signing, finalization, and broadcast where affected.
Distinguish data omitted from a returned result from data required internally
or by the next participant. Check supported paths with that data present and
absent where valid. An explicit rejection, an unusable transaction, and a valid
transaction paying an unintended recipient have different consequences.

For coin selection, fees, and change, trace ownership and spendability from
scripts and keys. Do not trust an external label or derivation path as proof
that an output is change. Check selected inputs, fee units and rounding, change
destination, and replacement behavior only where the PR changes them. Explain
the concrete payment or signing error rather than assuming every failed send
loses funds.

Check what survives interruption, restart, rescan, reorg, migration, and
partial writes where those paths are affected. Inspect whether restored or
loaded wallet state is complete and usable, not merely parseable. Distinguish
private keys and derivation state needed to spend from transaction metadata
that a rescan can reconstruct. Establish what the affected backup actually
contains; a rescan cannot recover missing keys.

For a funds or privacy claim, identify the user action or prior state, the
wallet invariant that changes, and the concrete consequence. Establish how
funds could be lost, become unspendable, be sent incorrectly, or how private
wallet data could be revealed. Report only concrete risks introduced by this
PR, tied to checkout evidence in the same adversarial discovery object.

For privacy, name the observer, what they learn, and the changed disclosure path.
Check affected address reuse, change identification, coin grouping, logs, and
broadcast behavior against the workflow's actual guarantees. Preserve ordinary
wallet regressions even when fund loss or a privacy violation is not established.
