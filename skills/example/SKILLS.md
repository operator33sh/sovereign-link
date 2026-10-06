---
name: example
description: "Demonstrates the SKILLS.md format — summarise a vault file and write a note."
version: 1.0.0
tools:
  - read_vault
  - write_vault
  - search_vault_semantic
dependencies: []
---

# Example Skill

This skill demonstrates how to read content from the vault, summarise it, and write a new note.

## Instructions

1. Use `search_vault_semantic` to find relevant content on the requested topic.
2. Read the top result with `read_vault`.
3. Summarise the content in 3–5 bullet points in Dutch.
4. Write the summary as a new vault note using `write_vault`, named `<datum>_samenvatting_<topic>.md`.
5. Report the note path to the user.

## Parameters

- **topic** *(required)*: the subject to search for and summarise.
- **target_dir** *(optional, default: `samenvattingen/`)*: vault subdirectory where the note is saved.

## Example invocation

> "Gebruik de example skill om een samenvatting te maken van alles over zelfzorg."
