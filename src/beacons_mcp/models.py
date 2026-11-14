"""Structured results returned by the tools (they become the tools' output schemas)."""

from pydantic import BaseModel, Field


class ProteinCandidate(BaseModel):
    accession: str = Field(description="UniProt accession, e.g. P69905.")
    entry_name: str = Field(description="UniProt entry name, e.g. HBA_HUMAN.")
    protein_name: str | None = Field(description="Recommended protein name.")
    gene_names: list[str] = Field(
        description="Gene names: the primary name first, then synonyms."
    )
    organism: str = Field(description="Organism, scientific name with common name.")
    reviewed: bool = Field(
        description="True for curated Swiss-Prot entries, false for unreviewed TrEMBL."
    )


class ProteinSearchResult(BaseModel):
    query: str = Field(description="The name that was searched for.")
    organism: str = Field(description="The organism filter that was applied.")
    candidates: list[ProteinCandidate] = Field(
        description="Ranked candidates, reviewed entries first."
    )
    total_matches: int | None = Field(
        description="How many entries UniProt matched in all, if it said."
    )
    confirmed_by_user: bool = Field(
        default=False,
        description=(
            "True when the user picked this protein in a dialog; "
            "then 'candidates' holds only their choice."
        ),
    )
    note: str = Field(description="What to do next with these candidates.")


class ProteinInfo(BaseModel):
    accession: str = Field(description="UniProt accession.")
    entry_name: str | None = Field(description="UniProt entry name.")
    sequence_length: int | None = Field(
        description="Length of the protein in residues."
    )


class ProviderTotal(BaseModel):
    provider: str = Field(description="Provider name as the Hub reports it.")
    models: int = Field(description="Number of models from this provider.")
    description: str | None = Field(
        description="One-sentence description of the provider, when known."
    )


class ModelSummary(BaseModel):
    provider: str = Field(description="Provider that serves the model.")
    model_id: str = Field(description="The provider's identifier for the model.")
    category: str = Field(
        description=(
            "How the model was made, as the Hub reports it, lower-cased: "
            "'experimentally determined', 'conformational ensemble', "
            "'template-based' or 'ab-initio'."
        )
    )
    residues: str = Field(
        description="UniProt residue range the model covers, as the Hub reports it, e.g. '2-142'."
    )
    span_percent: float | None = Field(
        description="Percent of the protein sequence between the first and last residue of the range."
    )
    resolution_angstrom: float | None = Field(
        description="Resolution in angstroms (experimental models only; lower is better)."
    )
    experimental_method: str | None = Field(
        description="Experimental method, e.g. 'X-RAY DIFFRACTION' (experimental models only)."
    )
    confidence_type: str | None = Field(
        description="Kind of confidence score, e.g. 'pLDDT' (predicted models only)."
    )
    confidence_score: float | None = Field(
        description="Average confidence score; what 'good' means depends on confidence_type."
    )
    model_url: str = Field(description="URL of the model file.")
    file_format: str | None = Field(
        description="File format the Hub reports for model_url, e.g. 'MMCIF' or 'PDB'."
    )
    page_url: str | None = Field(description="Web page describing the model.")
    conformers: int | None = Field(
        description="Number of conformers in the full ensemble (ensembles only)."
    )
    note: str | None = Field(description="Caveat about this model or its file, if any.")


class StructureResult(BaseModel):
    protein: ProteinInfo
    total_models: int = Field(
        description="Number of models found across all providers."
    )
    by_provider: list[ProviderTotal] = Field(
        description="Models per provider, largest first."
    )
    by_category: dict[str, int] = Field(description="Models per category.")
    models: list[ModelSummary] = Field(
        description="A selection of models, most of the protein first (see note)."
    )
    models_not_shown: int = Field(description="Models left out of 'models'.")
    note: str = Field(description="How to read this result.")
