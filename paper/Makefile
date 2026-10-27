PYTHON ?= python3
LATEXMK ?= latexmk

.PHONY: all assets clean arxiv

all: qfabric-paper.pdf

assets:
	$(PYTHON) ../scripts/generate-interconnect-assets.py
	$(PYTHON) ../scripts/generate-paper-assets.py
	$(PYTHON) ../scripts/generate-stage12-assets.py

qfabric-paper.pdf: qfabric-paper.tex references.bib assets
	$(LATEXMK) -pdf -interaction=nonstopmode -halt-on-error qfabric-paper.tex

clean:
	$(LATEXMK) -C qfabric-paper.tex

# Self-contained arXiv source bundle: arXiv compiles with pdflatex and the .bbl.
arxiv: qfabric-paper.pdf
	tar -czf qfabric-arxiv.tar.gz qfabric-paper.tex qfabric-paper.bbl references.bib \
		generated/*.tex generated/*.dat figures/testbed.jpg
