"""PDF validation and existing text-layer extraction; no image recognition."""


def pdf_module():
    import pymupdf
    return pymupdf


def inspect_pdf(raw):
    if not raw[:1024].lstrip().startswith(b'%PDF-'):
        raise ValueError('Ответ сервера не является PDF')
    with pdf_module().open(stream=raw,filetype='pdf') as doc:
        if doc.needs_pass or not 0<len(doc)<=150:
            raise ValueError('PDF защищён или превышает предел 150 страниц')
        texts=[page.get_text() for page in doc]
        return {'pages':len(doc),'text':'\n\f\n'.join(texts),'texts':texts,
                'metadata':doc.metadata or {}}
