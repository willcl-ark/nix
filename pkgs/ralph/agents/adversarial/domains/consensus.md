Consensus and validation profile: apply this only to changed consensus,
validation, script, serialization, chainstate, coins, kernel, or related helper
paths. It refines the adversarial review; do not treat it as a separate stage.

Identify the validation stage that changed. Header acceptance, transaction
admission to the mempool, block validation, and activation of the most-work
valid chain establish different facts. A nonstandard transaction can be valid
in a block; a valid header does not establish that its block is valid.

Compare base and head under the same parent chain and active consensus rules.
For an acceptance difference, name the block or transaction accepted at one
revision and rejected at the other, and the rule responsible. For a state
difference, trace the resulting UTXO set and chain tip. Check height, median
time past, deployment state, script flags, and coinbase maturity only where
they affect the changed path. An intentional activation boundary is not itself
consensus disagreement.

Follow affected ConnectBlock, DisconnectBlock, coins, serialization, or script
paths. Check that inputs cannot be spent twice, amounts and fees stay within
their required ranges, and disconnect/reconnect restores the expected coins
where those invariants are touched. For caches, establish which transaction
data, witness, prevouts, flags, and chain context determine the cached result.
A cache hit must establish the checks required by its caller.

Fully validated results under the same rules must not depend on cache warmth,
supported platform, or a history of restarts and reorgs. Where assumevalid or
snapshot chainstate is involved, identify which checks are skipped or deferred
and what establishes trust in their place. Check any background validation and
its completion conditions before claiming a bypass.

For mining changes, trace selection through final block construction. Check
dependency ordering, weight and sigop accounting, fees and coinbase value where
affected. Establish a reachable invalid template, stalled construction, or lost
fee opportunity; a change in transaction ordering alone proves none of these.

Report only claims grounded in a reachable call path and the checked-out
base/head behavior. Tie every profile finding to checkout evidence in the same
adversarial discovery object.
