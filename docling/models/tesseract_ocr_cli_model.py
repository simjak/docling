import io
import logging
import tempfile
from subprocess import PIPE, Popen
from typing import Iterable, Tuple

import pandas as pd

from docling.datamodel.base_models import BoundingBox, CoordOrigin, OcrCell, Page
from docling.datamodel.pipeline_options import TesseractCliOcrOptions
from docling.models.base_ocr_model import BaseOcrModel

_log = logging.getLogger(__name__)


class TesseractOcrCliModel(BaseOcrModel):

    def __init__(self, enabled: bool, options: TesseractCliOcrOptions):
        super().__init__(enabled=enabled, options=options)
        self.options: TesseractCliOcrOptions

        self.scale = 3  # multiplier for 72 dpi == 216 dpi.

        self._name = None
        self._version = None

        if self.enabled:
            try:
                self._get_name_and_version()

            except Exception as exc:
                raise RuntimeError(
                    f"Tesseract is not available, aborting: {exc} "
                    "Install tesseract on your system and the tesseract binary is discoverable. "
                    "The actual command for Tesseract can be specified in `pipeline_options.ocr_options.tesseract_cmd='tesseract'`. "
                    "Alternatively, Docling has support for other OCR engines. See the documentation."
                )

    def _get_name_and_version(self) -> Tuple[str, str]:

        if self._name != None and self._version != None:
            return self._name, self._version

        cmd = [self.options.tesseract_cmd, "--version"]

        proc = Popen(cmd, stdout=PIPE, stderr=PIPE)
        stdout, stderr = proc.communicate()

        proc.wait()

        # HACK: Windows versions of Tesseract output the version to stdout, Linux versions
        # to stderr, so check both.
        version_line = (
            (stdout.decode("utf8").strip() or stderr.decode("utf8").strip())
            .split("\n")[0]
            .strip()
        )

        # If everything else fails...
        if not version_line:
            version_line = "tesseract XXX"

        name, version = version_line.split(" ")

        self._name = name
        self._version = version

        return name, version

    def _run_tesseract(self, ifilename: str):

        cmd = [self.options.tesseract_cmd]

        if self.options.lang is not None and len(self.options.lang) > 0:
            cmd.append("-l")
            cmd.append("+".join(self.options.lang))
        if self.options.path is not None:
            cmd.append("--tessdata-dir")
            cmd.append(self.options.path)

        cmd += [ifilename, "stdout", "tsv"]
        _log.info("command: {}".format(" ".join(cmd)))

        proc = Popen(cmd, stdout=PIPE)
        output, _ = proc.communicate()

        # _log.info(output)

        # Decode the byte string to a regular string
        decoded_data = output.decode("utf-8")
        # _log.info(decoded_data)

        # Read the TSV file generated by Tesseract
        df = pd.read_csv(io.StringIO(decoded_data), sep="\t")

        # Display the dataframe (optional)
        # _log.info("df: ", df.head())

        # Filter rows that contain actual text (ignore header or empty rows)
        df_filtered = df[df["text"].notnull() & (df["text"].str.strip() != "")]

        return df_filtered

    def __call__(self, page_batch: Iterable[Page]) -> Iterable[Page]:

        if not self.enabled:
            yield from page_batch
            return

        for page in page_batch:
            ocr_rects = self.get_ocr_rects(page)

            all_ocr_cells = []
            for ocr_rect in ocr_rects:
                # Skip zero area boxes
                if ocr_rect.area() == 0:
                    continue
                high_res_image = page._backend.get_page_image(
                    scale=self.scale, cropbox=ocr_rect
                )

                with tempfile.NamedTemporaryFile(suffix=".png", mode="w") as image_file:
                    fname = image_file.name
                    high_res_image.save(fname)

                    df = self._run_tesseract(fname)

                # _log.info(df)

                # Print relevant columns (bounding box and text)
                for ix, row in df.iterrows():
                    text = row["text"]
                    conf = row["conf"]

                    l = float(row["left"])
                    b = float(row["top"])
                    w = float(row["width"])
                    h = float(row["height"])

                    t = b + h
                    r = l + w

                    cell = OcrCell(
                        id=ix,
                        text=text,
                        confidence=conf / 100.0,
                        bbox=BoundingBox.from_tuple(
                            coord=(
                                (l / self.scale) + ocr_rect.l,
                                (b / self.scale) + ocr_rect.t,
                                (r / self.scale) + ocr_rect.l,
                                (t / self.scale) + ocr_rect.t,
                            ),
                            origin=CoordOrigin.TOPLEFT,
                        ),
                    )
                    all_ocr_cells.append(cell)

            ## Remove OCR cells which overlap with programmatic cells.
            filtered_ocr_cells = self.filter_ocr_cells(all_ocr_cells, page.cells)

            page.cells.extend(filtered_ocr_cells)

            # DEBUG code:
            # self.draw_ocr_rects_and_cells(page, ocr_rects)

            yield page
