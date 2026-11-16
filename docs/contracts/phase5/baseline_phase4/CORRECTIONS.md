# Tint vector correction — pack revision 2.1.1

Issued October 30, 2026. This is the implementation direction for this corrected pack, not an alternative algorithm to choose between.

## Versions and precedence

- Pack revision: **2.1.1**; supersedes the preceding pack for this correction.
- Baseline specification: **2.1.0**, retained byte-for-byte in this ZIP.
- Ruleset: **2.0.0**, unchanged because Section 7.7's intended policy is unchanged.
- Suite manifest target: **2.1.1**, reflecting the corrected oracle and additional boundary vectors. The updated test-plan field and this correction supersede the baseline specification's suite target 2.1.0. This is not a claim that a runnable suite has been built or run.
- Wire schemas, API version, report format, unrelated policy vectors and business behavior are unchanged.

Use the Section 7.7 HSL lightness transform: for negative tint t, L' = L * (1 + t); otherwise L' = L * (1 - t) + t. Convert back to sRGB, clamp channels to their valid range, and round the final 0–255 channels to nearest integer with exact halves rounded upward. Do not switch to a per-channel RGB tint formula or ties-to-even rounding. Do not round HSL intermediates to integers.

## Fixed oracle values

Base 336699 has normalized HSL H = 7/12, S = 1/2, L = 2/5.

| Base | Tint | Exact RGB before final rounding | Required RGB | Superseded RGB |
|---|---|---|---|---|
| 336699 | -0.5 | (25.5, 51, 76.5) | **1A334D** | 19334D |
| 336699 | 0.5 | (140.25, 178.5, 216.75) | **8CB3D9** | 8CB2D9 |

The origin of the old values has not been established. Floating-point error is a possible cause, not a proven diagnosis.

Two additional fixed regression vectors distinguish half-up from truncation and ties-to-even:

| Base | Tint | Exact RGB before final rounding | Required RGB |
|---|---|---|---|
| 010101 | -0.5 | (0.5, 0.5, 0.5) | **010101** |
| 050505 | -0.5 | (2.5, 2.5, 2.5) | **030303** |

## Implementation and tester requirements

1. Replace the earlier local context pack and use these constants in both implementation regression tests and independent tester expectations. The corrected constants are in phase4-policy-vectors.json.
2. Make the arithmetic produce the specified mathematical half-up result. Merely selecting a half-up final rounding function is insufficient if earlier floating-point error moves an exact half below its boundary. Exact rational arithmetic is one valid implementation choice; any other strategy must preserve the same result without incorrectly rounding genuinely below-half values upward. Do not introduce an unspecified blanket epsilon.
3. Keep independent answer keys independent of production helper output. Add real fixture/handler coverage for the existing P4-RULE-COLOR-THEME-TINT assertion; expand grouped vectors into individually reported stable cases when freezing the executable suite, as required by the baseline.
4. Run the relevant tint, theme/indexed-color, contrast and applicable regression checks. Record actual results and remaining mismatches. Passing these vectors alone does not establish complete color-rule or Phase 4 acceptance.
5. Record pack revision 2.1.1, the pack/member hashes, actual executable manifest version, fixture hashes and each team's adoption under the existing adoption process. Publishing this pack does not assert that both teams have already adopted or deployed it.
6. Preserve historical audits, original answer-key artifacts and retained test observations. Do not rewrite their version labels or outcomes.

No PR, production code, deployed system or independent executable tester is changed by distributing this pack.
