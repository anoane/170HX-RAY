# Credits and attribution

Last verified: 2026-11-26 (Europe/Rome)

This is one significance-ordered list. It intentionally does not divide people
into “special” and “other” classes. A credit identifies a contribution; it does
not endorse every technical claim made by that person or project. Exact message
IDs, timestamps, confidence and corrections are retained in
[`credits-verification-2026-11-23.md`](PROVENANCE.md#workspace-only-evidence)
and
[`credits-attachment-provenance-2026-11-23.md`](PROVENANCE.md#workspace-only-evidence).

## Contributors and source authors

| Rank | Person or project author | Evidence-grounded contribution and qualification |
|---:|---|---|
| 1 | [Petri Krohn](https://github.com/PetriKrohn) (`PetriKrohn`; Discord `@petrikrohn_26497`) | CMP 170HX researcher and public chronicler; by his own timestamped account, a member of the seven-person group that produced the exploit PoC later documented in Jon Pry's paper. Speaker for [“FACEB13D exploit: Liberating the A100 beast inside Nvidia's CMP 170HX e-waste”](https://talks.mrmcd.net/2026/talk/ZDRJLV/); cmp.wiki lists him among its maintainers and main contributors. “Main public chronicler” is an editorial description, not a formal title, and he is not named as an author of Jon's paper. |
| 2 | [Jon Pry](https://github.com/jonpry) | Sole named author of *A Canary in the Crypto Mine*. Petri credits Jon with the first working ROP chain while describing the final PoC as the seven-person group's result. |
| 3 | [Kate Land](https://github.com/kate-land) (`@onemillionkates`, Discord nickname `kater`) | Discovered and published the GA100 voltage-fault-injection route, original `ga100vfi` implementation and fuse-map research. `kater` and Kate are one verified identity, not two contributors. |
| 4 | Cab | CMP 170HX VBIOS/cryptography contributor, directly credited by Amogh and in revision-pinned public VBIOS research. No fuller public identity was established. |
| 5 | [BlackSun](https://github.com/bsun0000) (`@blacksun.0`) | Firmware-cryptography and privilege-escalation co-researcher; public artifacts attribute debug-key work to BlackSun, and Humvee55 attributes a firmware trap escalation to him. |
| 6 | Humvee55 (`@scallion_9883`) | Major early CMP researcher credited with Gen2, hardware ECC/release, CMP 100-210 and firmware work. Membership in Petri's private seven is not independently established. |
| 7 | [Amogh Munikote](https://github.com/amoghmunikote) (`@_j3ns`) | Owner/maintainer of `cmpunlocker`, operator of its Discord community and active collaborator in the wider effort. |
| 8 | xPatrik (`@._.patrik`, Discord nickname `xPatrik`) | Published the one-card `CMPGlitch.zip` research archive and its measured conversion record. The exact original ZIP is attached to message [`1571680867695464580`](https://discord.com/channels/1518634379604136178/1518695289278566450/1571680867695464580); its LLM-generated material and one-card claims remain evidence to verify, not programming authority. |
| 9 | Mello Jello (`@mellojello_`; [JosiahWhite](https://github.com/JosiahWhite)) | Published `rp2040-glitch-tool`; `pico-ga100vfi` credits him with the concurrent 8-GB write-up and first I2C plus `PEX_RST` route. That priority is a downstream maintainer's explicit attribution. |
| 10 | `@dstoro` (supplied name: Danylo Storozhev) | Supplied the dated 2026-11-17 fuse captures/portable FF tooling and 2026-11-21 unlock tools. Artifact integrity is verified; the legal-name/handle mapping is provider-supplied and not independently confirmed by the public profile. |
| 11 | [matstatman](https://github.com/matstatman) and `jjb-afk` | Authors in `matstatman/gpcprobe`; matstatman also authored `hbmmon`. XRAY's GPL-2.0 kernel-helper lineage descends from gpcprobe's BAR/ioctl shape, with independent identity, ownership, bounds, serialization and restoration hardening. The upstream Python frontend was not copied. |
| 12 | [dalzyu](https://github.com/dalzyu) (DaLZy) and [Languorem](https://github.com/Languorem) (Arcanum) | Authors in `pico-ga100vfi`, the audited RP2350/Pico adaptive glitch-search implementation. Its results and classifier were treated adversarially rather than as automatic write authority. |
| 13 | [portz3r0](https://github.com/portz3r0) (Raz) | Author/maintainer of `pico-ga100vfi-console`, used as a current console, reader, search, exact-but-recipe-scoped SS0/SS1 IFF comparison and guard-design source. The 2026-11-26 head makes classifier mutation opt-in and adds VRM-unlock refusal, campaign rollback evidence and a default inter-shot cooldown after a reported fatigue event; these are credited as implementation/field safeguards, not universal electrical qualification. Local metadata also records inherited dalzyu/Languorem work and automation identities; automation names are not credited as people. |
| 14 | Samuel Hayden (`@samuel_hayden.`) | Authored the four dated 2026-11-17 Discord warnings to use empty FF rows, preserve a boot-record buffer and revert tail, account for append-only corrections, and reserve space to re-disable bad SMs. Those general cautions are sound; they do not validate the later incident-specific recovery theory that quoted them. |
| 15 | RazK (`port00`) | Uploaded the exact `message.txt` preserved as `ff_message3.txt` in Discord message [`1572044176890011789`](https://discord.com/channels/1518634379604136178/1518695289278566450/1572044176890011789). This proves uploader/custody, not authorship of the wrapper narrative. |
| 16 | [sayhellotojungle](https://github.com/sayhellotojungle) and commit author `alongo` | Repository owner and recorded author of `cmp170hx-ffcalc`, used as a public FF-calculation and sample-data comparison source. Its replay, allocation and experimental whole-word assumptions are not burner authority. |
| 17 | [kprinssu](https://github.com/kprinssu) | Author of `ga100-ff-tools`, used for adversarial planner/burner comparison and, under its retained GPL terms, for the live NVENC/NVDEC driver-API session probe. |
| 17a | [repeaterdroid](https://github.com/repeaterdroid) (commit author `repeaterdroid`) | Published the 2026-11-22 one-card SYS_PIPE/MIG, NVDEC, NVJPG and OFA append/function report. It materially raised coordinate confidence while leaving the individual bit-146/bit-371 attribution and independent raw-campaign sealing unresolved. |
| 18 | [Andy Nguyen](https://github.com/ahnguyen17) (`ahnguyen17`) | Author of `ga100-fuse-re`, whose raw two-card material and hypotheses were separately audited and retained only with explicit corrections and uncertainty. |
| 19 | [Lxtend / lixiangwuxian](https://github.com/lixiangwuxian) | Author of `170hx-rir-tool` and `170hx_tips`, used for RIR, override and community PCIe observations with SKU/specimen limits preserved. |
| 20 | [darkmaniac7](https://github.com/darkmaniac7) | Author of `cmp170hx-8gb-vfi-tools`, used for difficult-card timing, classifier, autonomous-search and benchmark-method evidence; its single-fixture values are not portable constants. |
| 21 | [duggasco](https://github.com/duggasco) | Authored public NVIDIA probe/fuse resources directly referenced by Amogh and Petri, including `probe.sh` and CMP 100-210 research. |
| 22 | orrorin (`@_orrorin`) | Posted the public RIR fuse-burn proposal and encoding questions later corrected and discussed in the shared thread. |
| 23 | mrpandagod (`@djpandaman`) | Answered and validated record-encoding semantics in the public RIR-plan thread and supplied important disabled-versus-defective field observations. |
| 24 | meatsus | Alongside Petri in the dated discussion, explained why an A100 VBIOS cross-flash fails when image identity does not match fused device identity. |
| 25 | Rubbra_F (`@rubbra_f`) | Credited by Petri as the first person to put the pieces together into a reproducible compute unlock. This remains Petri's explicit attribution rather than an independently reconstructed priority claim. |
| 26 | [Zagitta](https://github.com/Zagitta) and recorded `gpu-unlock` contributors Khoa Truong, Noocktall Klown S., Simon, jesvs, lemonaide152, randomizedcoder and viwe-monai | Authored the audited volatile/reapplied ECC, topology and PCIe software-unlock implementation. Commit metadata also contains an automation identity (`Claude`), which is not represented as a human contributor. |
| 27 | [Chris Lee](https://github.com/clee) (`clee`) | Maintains the Pico/KB2040 ports used as board/transport references; the history also preserves inherited dalzyu work. |
| 28 | [dev-boz](https://github.com/dev-boz) (Byron Warnich) | Author of `cmp170hx-testsuite`, used for post-change HBM/SM/topology validation while preserving its exact-SM-coverage limitations. |
| 29 | [EthanZhangCN](https://github.com/EthanZhangCN) (Ethan Zhang) | Author of `cmp170hx-final-crack`, an aggregated end-to-end source whose claims and fail-open planning behavior were independently checked. |
| 30 | [Panchovix](https://github.com/Panchovix) | Author of the Linux NVIDIA VBIOS Vix Loader and related editor/loader work, used as the audited in-memory VBIOS transport reference. |
| 31 | [JRex286](https://gist.github.com/JRex286) | Author of the linked Ampere/GA100 fuse-register gist used as a comparative empirical map, not an authoritative physical descriptor. |
| 32 | [Consensus-Protocol](https://github.com/Consensus-Protocol) and the cmp.wiki maintainers/contributors | Maintained the public CMP 170HX hardware wiki and the later community index/archive. These mutable secondary sources were used to locate primary messages and artifacts; their claims were not accepted merely because they were documented. |
| 33 | [ProjectPhysX / niconiconi](https://github.com/ProjectPhysX) (Dr. Moritz Lehmann) | Earlier independent 2023 no-FMA/throttling research, credited separately from the later issue and unlock work. |
| 34 | [jetcat8848](https://github.com/jetcat8848) | Opened NVIDIA-patcher issue #73 and brought the CMP 170HX FMA/workaround discussion to wider attention; did not originate the earlier no-FMA finding. |
| 35 | [dartraiden](https://github.com/dartraiden) | Maintainer of `NVIDIA-patcher`, whose issue #73 became a primary public discussion and artifact trail for the CMP effort. |
| 36 | [arabel1a](https://github.com/arabel1a) | Documented the DP4A throttle and published a llama.cpp native-unpacking workaround. |
| 37 | [eastmoe](https://github.com/eastmoe) | Authored `CMPGPU-patch-script`, an earlier software-patch aggregation referenced in the community record; it is not a hardware unlock. |
| 38 | Zorg33 (`@zorg33`) | Published multiple hardware/test observations, including x16 capacitor-mod measurements, topology/performance reports and defect cautions. Individual unmeasured claims, including NVLink reports, retain their original qualification. |
| 39 | PJ | Community tester with dated driver-unlock and hardware/community reports. Only the specific linked observations are credited; no broader role is inferred. |
| 40 | kostakoff | Dumped and publicly attached a CMP 170HX VBIOS using `nvflash`. |
| 41 | InnovativeOSS117 | Performed cross-VBIOS experiments on an unreleased GA100/PG509 card described by its owner as ES-like. |
| 42 | Big Ptoughneigh (`@bigptoughneigh`) | Published detailed Falcon/firmware reverse-engineering and silicon-test discussions in the clean-room record. Individual LLM-assisted interpretations remain tied to their raw captures rather than treated as authority. |
| 43 | `68x0` / 68x | Reported and attached the dated modified-`gpu-unlock` CMP-path result showing 80 GiB, 126 SMs and Gen4 after prior glitch/fuse work. The report is specimen-specific and does not establish a universal fuse image. |
| 44 | Colo2442 (`@colo2442`) | Reported A/B testing individual SM pairs and reaching 126 working SMs while isolating one failing pair in a defective-marked GPC; useful evidence for pair-granular validation, not permission to ignore defect masks. |
| 45 | Starling | Supplied the explicit 48-die × 2-GiB physical-capacity reasoning and a four-defective-FBP/64-GiB specimen report used to distinguish installed density from addressable capacity. |
| 46 | angrykirc | Contributed the dated observation that two defective FBPs imply an 80-GiB ceiling on the discussed 16-Gb-die board geometry; retained as a field report and cross-check. |
| 47 | domrockt2 | Published four-card topology/SM estimates while explicitly labeling them estimates rather than demonstrated unlocks; that qualification is preserved. |
| 48 | truvv0r | Reported GA102 programming failures and later corrected his causal account to a Python value bug. The chronology is retained as a write-validation caution, not GA100 electrical proof. |
| 49 | Alex1997 | Supplied an early in-memory A100-VBIOS negative test showing that loading the VBIOS alone did not unlock a stock card. |
| 50 | Kulak0v | Supplied A100-conversion/media observations later bounded by raw API negatives; credited for the experiment, not for an unverified claim that NVDEC/NVJPG were working. |
| 51 | ichundes (`@ichundes`) | Supplied useful board/glitch wiring imagery and hardware observations reused by public Pico documentation. |
| 52 | CUTE (`@cute_sc2`) | Contributed capacitor/link observations and a reported zero-defect specimen. The latter remains a qualified report rather than a captured 96-GiB proof. |
| 53 | `mg94301-2007` | Community-requested acknowledgement only. No direct post, repository or test artifact establishing the requested “hardware tester” role was found, and the named GitHub URL returned 404 on the verification date. No contribution is asserted pending primary evidence. |

Repository ownership or a commit author line establishes authorship/maintenance
of that repository or commit, not correctness of every claim inside it. Likewise,
a Discord upload proves the account that uploaded a file; it does not, by itself,
prove who wrote the file.

## Attachment provenance that must travel with the credit

- `CMPGlitch.zip`: original SHA-256
  `e395005c6f163ff858d5902b4ce8025ce80b58dc8994f5d3826608f7eced21f5`,
  3,560,291 bytes, attached by xPatrik at 2026-11-16 00:18:39 UTC. The
  cmp.wiki publication ZIP is a deliberately modified copy with SHA-256
  `2ab2e92effeca688acf59cfc04ae587cf3d01f16795d50fcbdbeade13e4a5e76`.
- `ff_message3.txt`: SHA-256
  `7ca85220d8de760b06d53de7d2c8e19d35fd7c5be4d6e2eda7ca6ecaf2d91833`,
  exactly equal to RazK/`port00`'s `message.txt` attachment at 2026-11-17
  00:22:18 UTC. Its quoted warnings are Samuel Hayden messages
  `1572040341685534783`, `1572040382600974407`, `1572040555687313430`
  and `1572041469873619067`. The surrounding incident/recovery interpretation
  remains rejected by the line-level technical audit; attribution is not
  corroboration.

## Licensing and trust boundary

The kernel sources retain the GPL-2.0 attribution required by their
`matstatman/gpcprobe` lineage. XRAY otherwise uses the listed projects as research,
comparison and test evidence according to each source's license; naming a source
does not silently import unlicensed code. Exact pins, licenses, dates and known
defects live in
[`SOURCE_INDEX.md`](PROVENANCE.md#workspace-only-evidence) and the canonical
[research runbook](PROVENANCE.md#workspace-only-evidence).
