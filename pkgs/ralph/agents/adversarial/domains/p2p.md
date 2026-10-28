P2P and resource-abuse profile: apply this only to changed peer-controlled
inputs, relay behavior, request tracking, admission policy, eviction, resource
limits, and repeated work. It refines the adversarial review; do not treat it
as a separate stage.

Trace peer-controlled size, order, timing, and repetition through message
parsing, validation, request tracking, caches, and disconnect cleanup. Establish
the negotiated protocol behavior and limits that apply before the changed code
runs. Do not assume a direct call with arbitrary bytes is reachable over P2P.

For relay and requests, identify whether state is keyed by txid, wtxid, block
hash, or peer. Separate peer-local state from shared rejection and request
tracking. Check whether a peer can suppress an honest peer's response, retain
ownership of a request without delivering data, or make an invalid variant
prevent later processing of valid data. Trace timeout, disconnect, expiry,
reannouncement, and reorg recovery before claiming a persistent stall.

State required inbound or outbound access, peer count, and any proof-of-work
or fee cost. For header chains, identify the fork point, required work, and
the presync or admission checks reached. Distinguish a transaction's offered
fee from fees the attacker actually pays when transactions confirm. Account
for shared limits before multiplying a per-peer cost. Check whether memory is
transient or retained, whether disconnect releases it, and whether retries
accumulate work or state. Label estimates as estimates; static review cannot
establish measured CPU time, memory, disk I/O, or bandwidth. Count expensive
operations and identify the thread, queue, or lock they occupy. Explain what
useful work is delayed and why input bounds, rate limits, or deduplication
fail to contain it. Extra work or amplification alone does not establish
denial of service. Distinguish CPU work from waiting on cold storage.

For address relay, connection selection, and eviction, trace whether an attacker
can cheaply displace honest peers or prevent the node from obtaining blocks.
For proxy, network-selection, or private-broadcast changes, establish the exact
documented guarantee and its exceptions at this revision. Trace the affected
connection, DNS lookup, or announcement; do not assume all connections have the
same policy or promise anonymity beyond that contract.

For a resource-abuse or peer-behavior claim, identify the reachable peer action,
the bound or progress guarantee that changed, and the concrete consequence.
Distinguish a remote-triggerable cost from local maintenance work. Report only
concrete risks introduced or worsened by this PR, tied to checkout evidence in
the same adversarial discovery object.
