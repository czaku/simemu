# create.py reintroduces greedy device matching (P2/P3, code-confirmed)
create.py:277-283 _fuzzy_match: exact match first, else FIRST partial substring match.
`create_ios(name, "iPhone 17", "iOS 27")` -> _fuzzy_match("iphone 17", devicetypes) returns the
first of {iPhone 17, iPhone 17 Pro, iPhone 17 Pro Max, iPhone 17e} -> can create the WRONG device
type. This is the exact greedy bug discover.py fixed in T-LU-263 (_resolve_device_selector), but
the fix was not applied to create.py. Same class for runtime_query ("iOS 2" matches 26.x/27.x).
GOOD: create.py is otherwise dynamic — reads simctl devicetypes+runtimes live, so it supports
NEW iPhone/iOS-27 device types & runtimes with no hardcoded list. iOS-27 readiness gap is in
SELECTION (version-preference), not creation.
FIX: reuse the anchored exact-or-unambiguous matcher from discover.py in create.py; reject
ambiguous queries instead of silently picking the first.
