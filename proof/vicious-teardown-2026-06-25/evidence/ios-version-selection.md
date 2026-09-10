# iOS version selection defects — runtime-proven 2026-06-25 (Xcode 26.5)

## F: unversioned `claim ios` does not prefer newest runtime (P1, REAL)
UNVERSIONED claim picked: iOS 26.5 (older) when 26.5 + 27.0 both present.
Reversing simctl list order flipped the choice -> nondeterministic by simctl JSON order.
discover.py:526-548 _score has no version-descending tiebreak; version_score=0 for all when os_version is None.
Impact: when iOS 27 ships beside iOS 26, an agent asking for "ios" can silently get iOS 26.

## F: explicit `--version 26` picks 26.4 over 26.5 (P2, REAL)
No newest-among-matches tiebreak; returns older patch.

## F: garbage/loose version silently matched via substring (P2, REAL)
--version 2   -> iOS 26.5   (no validation)
--version 7   -> iOS 17.7   (substring matches both 17.7 and 27.0)
--version 2.5 -> iOS 26.5   (2.5 substring-matches 26.5)
discover.py:538 `spec.os_version in runtime_lower` substring match.

FIX: parse runtime label -> (major,minor) int tuple; match os_version on major or major.minor
equality (not substring); add descending-version as primary tiebreak in _score so newest wins.
