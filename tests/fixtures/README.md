# Test fixtures

Real responses captured on 2026-11-14 during the Phase 0 spike, trimmed for size.
They pin the shapes the server depends on; they are not a statement of what the
services return today.

| File | Source | Trimming |
|---|---|---|
| `uniprot_search_cbp1_human_gene.json` | `rest.uniprot.org/uniprotkb/search?query=gene:CBP1 AND organism_id:9606` | none (`fields=accession,id,protein_name,gene_names,organism_name,reviewed`) |
| `uniprot_search_cbp1_human_freetext.json` | same, query `CBP1 AND organism_id:9606` (free text, noisy) | none |
| `uniprot_search_cbp1_any_organism.json` | same, query `gene:CBP1`, `size=10` | none |
| `uniprot_search_hemoglobin_human.json` | same, query `(gene:HBB OR protein_name:hemoglobin) AND organism_id:9606`, `size=5` | none |
| `uniprot_search_no_match.json` | same, query `gene:ZZZZNOTAGENE AND organism_id:9606` | none |
| `hub_summary_P69905.json` | `GET {hub}/uniprot/summary/P69905.json` | 439 -> 13 items (max 2 per provider, plus one more SASBDB item); `entities` capped at 3 |
| `hub_summary_Q92793.json` | `GET {hub}/uniprot/summary/Q92793.json` | 211 -> 15 items, keeping the two-segment PDBe entry `9h0k` and PED items whose `coverage` disagrees with their span; `entities` capped at 3 |
| `hub_summary_P37840.json` | `GET {hub}/uniprot/summary/P37840.json` | 320 -> 7 items, keeping three PED ensembles with different conformer counts and coverage; `entities` capped at 3 |
| `hub_health.json` | `GET {hub}/health/` | none |
| `hub_openapi_summary_params.json` | `GET {hub}/openapi.json` | only `info`, `servers` and the parameters of the summary endpoint |
| `registry_providers.json` | `3D-Beacons/3d-beacons-registry`, `resources/registry.json` | providers only; logo and service URLs dropped |

`{hub}` is `https://www.ebi.ac.uk/pdbe/pdbe-kb/3dbeacons/api`.

Because the Hub summaries are trimmed, the item counts inside them do not match
the real totals (439, 211 and 320). Tests must not treat them as full responses.
