"""Pydantic models for schema.org JSON-LD metadata."""

from pydantic import BaseModel, Field
from typing import Optional, List

class PersonOrOrganization(BaseModel):
    """Schema.org Person or Organization reference."""
    type: str = Field(alias="@type")
    name: str
    role: Optional[str] = None

class DefinedTerm(BaseModel):
    """Schema.org DefinedTerm entry."""
    type: str = Field(alias="@type")
    name: Optional[str] = None
    termCode: Optional[str] = None
    inDefinedTermSet: Optional[str] = None
    
class CreativeWork(BaseModel):
    """Schema.org CreativeWork reference."""
    type: str = Field(alias="@type")
    name: Optional[str] = None
    author: Optional[List[PersonOrOrganization]] = None
    inLanguage: Optional[str] = None
    url: Optional[List[str]] = None


class Book(BaseModel):
    """Schema.org Book metadata model."""
    context: str = Field(alias="@context")
    type: str = Field(alias="@type")

    # Core metadata
    name: Optional[str] = None
    author: Optional[List[PersonOrOrganization]] = None
    contributor: Optional[List[PersonOrOrganization]] = None
    publisher: Optional[PersonOrOrganization] = None
    datePublished: Optional[str] = None
    isbn: Optional[List[str]] = None
    inLanguage: Optional[str] = None
    description: Optional[str] = None
    numberOfPages: Optional[int] = None
    bookEdition: Optional[int] = None
    about: Optional[List[DefinedTerm]] = None

    # Optional enhancements
    genre: Optional[List[str]] = None
    audience: Optional[str] = None
    accessMode: Optional[str] = None
    accessModeSufficient: Optional[List[str]] = None
    suggestedMinAge: Optional[int] = None
    isBasedOn: Optional[CreativeWork] = None 
    
    class Config:
        populate_by_name = True


class BookPatch(Book):
    """Partial Book payload used for metadata patching."""

    context: Optional[str] = Field(alias="@context", default=None)
    type: Optional[str] = Field(alias="@type", default=None)
