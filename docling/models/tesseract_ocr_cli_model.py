import io
import logging
import tempfile
from subprocess import DEVNULL, PIPE, Popen
from typing import Iterable, Optional, Tuple

import pandas as pd
from docling_core.types.doc import BoundingBox, CoordOrigin

from docling.datamodel.base_models import Cell, OcrCell, Page
from docling.datamodel.document import ConversionResult
from docling.datamodel.pipeline_options import TesseractCliOcrOptions
from docling.datamodel.settings import settings
from docling.models.base_ocr_model import BaseOcrModel
from docling.utils.profiling import TimeRecorder

_log = logging.getLogger(__name__)


class TesseractOcrCliModel(BaseOcrModel):

    def __init__(self, enabled: bool, options: TesseractCliOcrOptions):
        super().__init__(enabled=enabled, options=options)
        self.options: TesseractCliOcrOptions

        self.scale = 3  # multiplier for 72 dpi == 216 dpi.

        self._name: Optional[str] = None
        self._version: Optional[str] = None

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
            return self._name, self._version  # type: ignore

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

        if self.options.lang and len(self.options.lang) > 0:
            cmd.extend(["-l", "+".join(self.options.lang)])
        if self.options.path:
            cmd.extend(["--tessdata-dir", self.options.path])

        with tempfile.NamedTemporaryFile(suffix=".tsv") as tsv_file:
            # Modify command to write output to a TSV file instead of stdout
            cmd.extend([ifilename, tsv_file.name, "tsv"])
            _log.info("command: {}".format(" ".join(cmd)))

            # Capture stderr to handle any Tesseract errors
            proc = Popen(cmd, stdout=PIPE, stderr=PIPE)
            stdout, stderr = proc.communicate()

            if proc.returncode != 0:
                error_message = stderr.decode("utf-8")
                raise RuntimeError(f"Tesseract failed with error: {error_message}")

            # Read TSV output from the temporary file
            tsv_file.seek(0)
            decoded_data = tsv_file.read().decode("utf-8")

        # Parse the TSV data into a DataFrame
        df = pd.read_csv(io.StringIO(decoded_data), sep="\t")

        # Filter rows that contain actual text
        df_filtered = df[df["text"].notnull() & (df["text"].str.strip() != "")]

        return df_filtered

    def __call__(
        self, conv_res: ConversionResult, page_batch: Iterable[Page]
    ) -> Iterable[Page]:

        if not self.enabled:
            yield from page_batch
            return

        for page in page_batch:
            assert page._backend is not None
            if not page._backend.is_valid():
                yield page
            else:
                with TimeRecorder(conv_res, "ocr"):

                    ocr_rects = self.get_ocr_rects(page)

                    all_ocr_cells = []
                    for ocr_rect in ocr_rects:
                        # Skip zero area boxes
                        if ocr_rect.area() == 0:
                            continue
                        high_res_image = page._backend.get_page_image(
                            scale=self.scale, cropbox=ocr_rect
                        )

                        with tempfile.NamedTemporaryFile(
                            suffix=".png", mode="w"
                        ) as image_file:
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

                    # Post-process the cells
                    page.cells = self.post_process_cells(all_ocr_cells, page.cells)

                # DEBUG code:
                if settings.debug.visualize_ocr:
                    self.draw_ocr_rects_and_cells(conv_res, page, ocr_rects)

                yield page
