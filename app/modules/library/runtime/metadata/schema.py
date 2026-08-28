"""Pydantic models for the supported schema.org JSON-LD subset."""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class PersonOrOrganization(BaseModel):
    """Schema.org Person or Organization reference."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    type: Literal["Person", "Organization"] = Field(alias="@type")
    name: str


class ContributorRole(BaseModel):
    """Schema.org Role wrapper for a contributor relationship."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    type: Literal["Role"] = Field(alias="@type")
    roleName: str
    contributor: PersonOrOrganization


class DefinedTermSet(BaseModel):
    """Named schema.org term set."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    type: Literal["DefinedTermSet"] = Field(alias="@type")
    name: str
    url: Optional[str] = None


class DefinedTerm(BaseModel):
    """Schema.org DefinedTerm entry."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    type: Literal["DefinedTerm"] = Field(alias="@type")
    name: Optional[str] = None
    termCode: Optional[str] = None
    inDefinedTermSet: str | DefinedTermSet


class Audience(BaseModel):
    """Schema.org audience reference."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    type: Literal["Audience", "EducationalAudience", "PeopleAudience"] = Field(
        alias="@type"
    )
    audienceType: Optional[str] = None
    suggestedMinAge: Optional[StrictInt] = None
    suggestedMaxAge: Optional[StrictInt] = None


class AccessModeItemList(BaseModel):
    """One sufficient combination of schema.org access modes."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    type: Literal["ItemList"] = Field(alias="@type")
    itemListElement: list[Literal["auditory", "tactile", "textual", "visual"]]
    description: Optional[str] = None


class CreativeWork(BaseModel):
    """Schema.org CreativeWork reference."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    type: str = Field(alias="@type")
    name: Optional[str] = None
    author: Optional[list[PersonOrOrganization]] = None
    inLanguage: Optional[str] = None
    url: Optional[list[str]] = None


class Book(BaseModel):
    """Supported document-oriented schema.org CreativeWork metadata."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    context: str = Field(alias="@context")
    type: Literal[
        "Article",
        "Book",
        "CreativeWork",
        "HowTo",
        "Legislation",
        "NewsArticle",
        "Newspaper",
        "PublicationIssue",
        "Report",
        "ScholarlyArticle",
        "Thesis",
    ] = Field(alias="@type")

    # Core metadata
    name: Optional[str] = None
    author: Optional[list[PersonOrOrganization]] = None
    contributor: Optional[list[PersonOrOrganization | ContributorRole]] = None
    editor: Optional[list[PersonOrOrganization]] = None
    translator: Optional[list[PersonOrOrganization]] = None
    illustrator: Optional[list[PersonOrOrganization]] = None
    publisher: Optional[PersonOrOrganization] = None
    datePublished: Optional[str] = None
    isbn: Optional[list[str]] = None
    inLanguage: Optional[str] = None
    description: Optional[str] = None
    numberOfPages: Optional[StrictInt] = None
    bookEdition: Optional[str] = None
    about: Optional[list[DefinedTerm]] = None

    # Optional enhancements
    genre: Optional[list[str]] = None
    audience: Optional[Audience | list[Audience]] = None
    accessMode: Optional[list[Literal["auditory", "tactile", "textual", "visual"]]] = (
        None
    )
    accessModeSufficient: Optional[list[AccessModeItemList]] = None
    isBasedOn: Optional[CreativeWork] = None


class BookPatch(Book):
    """Partial Book payload used for metadata patching."""

    context: Optional[str] = Field(alias="@context", default=None)
    type: Optional[str] = Field(alias="@type", default=None)
