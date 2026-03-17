# Studienarbeit (DHBW Stuttgart Campus Horb) - LaTeX Projekt

Dieses Projekt ist ein sofort nutzbares Grundgeruest fuer eine Studienarbeit.

## Projektstruktur

- `main.tex` - Hauptdatei
- `config/` - Metadaten und Paketkonfiguration
- `chapters/` - Kapiteldateien
- `appendix/` - Anhang
- `bibliography/references.bib` - Literaturdatenbank (BibLaTeX)
- `figures/` - Abbildungen
- `tables/` - Tabellenmaterial
- `latexmkrc` - Build-Konfiguration fuer `latexmk`

## Schnellstart

1. Titel- und Personendaten in `config/meta.tex` anpassen.
2. Inhalte in den Dateien unter `chapters/` schreiben.
3. Quellen in `bibliography/references.bib` eintragen.
4. In VS Code mit LaTeX Workshop bauen oder im Terminal:

```powershell
latexmk -pdf -interaction=nonstopmode -synctex=1 main.tex
```

## Hinweise

- Sprache: Deutsch (`ngerman`)
- Zitation: `biblatex` mit `biber`
- Kompatibel mit TeX Live / MiKTeX
