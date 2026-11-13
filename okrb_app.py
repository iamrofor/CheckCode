#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ОКРБ Catalog — десктопное приложение для учёта и поиска кодов ОКРБ.

Стек: Python 3.10+, PyQt6, SQLite3 (встроен), PyMuPDF (fitz).

Установка зависимостей:
    pip install PyQt6 PyMuPDF

Запуск:
    python okrb_app.py
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import traceback
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    fitz = None  # обработка при импорте PDF покажет сообщение об ошибке


DB_FILENAME: str = "okrb_catalog.db"

# Название столбца-флага документа -> человекочитаемое имя
DOCUMENT_FLAGS: Tuple[str, ...] = (
    "in_decree_80",
    "in_168_app1",
    "in_168_app2",
    "in_168_app3",
)

PAGE_SIZE: int = 100  # сколько записей показывать при запуске для быстродействия


# ---------------------------------------------------------------------------
# Модель данных
# ---------------------------------------------------------------------------
@dataclass
class OkrbRecord:
    """Одна запись каталога ОКРБ."""

    okrb_code: str
    product_name: str
    in_decree_80: int = 0
    in_168_app1: int = 0
    in_168_app2: int = 0
    in_168_app3: int = 0

    def inclusion_text(self) -> str:
        """Читаемая строка вида 'Пост. 80; Пост. 168 (Прил. 1, 3)'."""
        parts: List[str] = []
        if self.in_decree_80:
            parts.append("Пост. 80")

        apps: List[str] = []
        if self.in_168_app1:
            apps.append("1")
        if self.in_168_app2:
            apps.append("2")
        if self.in_168_app3:
            apps.append("3")
        if apps:
            parts.append(f"Пост. 168 (Прил. {', '.join(apps)})")

        return "; ".join(parts) if parts else "Не найдено в реестрах"


# ---------------------------------------------------------------------------
# Работа с базой данных
# ---------------------------------------------------------------------------
class DatabaseManager:
    """Инкапсулирует все операции с SQLite-базой каталога ОКРБ."""

    def __init__(self, db_path: str = DB_FILENAME) -> None:
        self.db_path: str = db_path
        self.conn: sqlite3.Connection = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        try:
            with self.conn:
                self.conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS okrb_catalog (
                        okrb_code     TEXT PRIMARY KEY,
                        product_name  TEXT NOT NULL DEFAULT '',
                        in_decree_80  INTEGER NOT NULL DEFAULT 0,
                        in_168_app1   INTEGER NOT NULL DEFAULT 0,
                        in_168_app2   INTEGER NOT NULL DEFAULT 0,
                        in_168_app3   INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_okrb_name "
                    "ON okrb_catalog(product_name)"
                )
        except sqlite3.Error as e:
            raise RuntimeError(f"Ошибка инициализации базы данных: {e}") from e

    # -- CRUD ---------------------------------------------------------------
    def upsert(self, rec: OkrbRecord) -> None:
        """INSERT OR REPLACE записи (по первичному ключу okrb_code)."""
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO okrb_catalog
                        (okrb_code, product_name,
                         in_decree_80, in_168_app1, in_168_app2, in_168_app3)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rec.okrb_code.strip(),
                        rec.product_name.strip(),
                        int(rec.in_decree_80),
                        int(rec.in_168_app1),
                        int(rec.in_168_app2),
                        int(rec.in_168_app3),
                    ),
                )
        except sqlite3.Error as e:
            raise RuntimeError(f"Ошибка сохранения записи '{rec.okrb_code}': {e}") from e

    def update_flags(self, okrb_code: str, flag_column: str) -> None:
        """Проставить 1 в указанный столбец-флаг, не трогая остальные данные.

        Если записи ещё нет — она создастся с пустым наименованием
        (данные о коде из другого документа не потеряются).
        """
        if flag_column not in DOCUMENT_FLAGS:
            raise ValueError(f"Недопустимый столбец-флаг: {flag_column}")
        try:
            with self.conn:
                self.conn.execute(
                    f"""
                    INSERT INTO okrb_catalog (okrb_code, product_name, {flag_column})
                    VALUES (?, '', 1)
                    ON CONFLICT(okrb_code) DO UPDATE SET {flag_column} = 1
                    """,
                    (okrb_code,),
                )
        except sqlite3.Error as e:
            raise RuntimeError(f"Ошибка обновления флага {flag_column}: {e}") from e

    def delete(self, okrb_code: str) -> bool:
        try:
            with self.conn:
                cur = self.conn.execute(
                    "DELETE FROM okrb_catalog WHERE okrb_code = ?", (okrb_code,)
                )
            return cur.rowcount > 0
        except sqlite3.Error as e:
            raise RuntimeError(f"Ошибка удаления записи '{okrb_code}': {e}") from e

    def get(self, okrb_code: str) -> Optional[OkrbRecord]:
        cur = self.conn.execute(
            "SELECT * FROM okrb_catalog WHERE okrb_code = ?", (okrb_code,)
        )
        row = cur.fetchone()
        return self._row_to_record(row) if row else None

    # -- Поиск / выборка ----------------------------------------------------
    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> OkrbRecord:
        return OkrbRecord(
            okrb_code=row["okrb_code"],
            product_name=row["product_name"],
            in_decree_80=row["in_decree_80"],
            in_168_app1=row["in_168_app1"],
            in_168_app2=row["in_168_app2"],
            in_168_app3=row["in_168_app3"],
        )

    def search(
        self,
        code_query: str = "",
        name_query: str = "",
        limit: Optional[int] = PAGE_SIZE,
        offset: int = 0,
    ) -> List[OkrbRecord]:
        """Поиск по коду и наименованию (логика AND). Пустые условия игнорируются.

        limit=None — без ограничения (для фильтрации всей БД).
        """
        clauses: List[str] = []
        params: List[str] = []
        if code_query.strip():
            clauses.append("okrb_code LIKE ?")
            params.append(f"%{code_query.strip()}%")
        if name_query.strip():
            clauses.append("product_name LIKE ?")
            params.append(f"%{name_query.strip()}%")

        sql = "SELECT * FROM okrb_catalog"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY okrb_code"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([str(limit), str(offset)])

        try:
            cur = self.conn.execute(sql, params)
            return [self._row_to_record(r) for r in cur.fetchall()]
        except sqlite3.Error as e:
            raise RuntimeError(f"Ошибка поиска: {e}") from e

    def count_all(self) -> int:
        cur = self.conn.execute("SELECT COUNT(*) AS c FROM okrb_catalog")
        return int(cur.fetchone()["c"])

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass


# ---------------------------------------------------------------------------
# Парсер PDF
# ---------------------------------------------------------------------------
class PDFParser:
    """Извлекает коды ОКРБ и наименования товаров из текста PDF.

    Ожидаемый формат фрагментов: "... код товара ... 01.01.01.000 ..." —
    текст, идущий сразу после кода до следующего кода или конца
    строки/абзаца, считается наименованием товара.
    """

    # Базовый regex по ТЗ. К нему добавлен негативный lookahead, чтобы код
    # не «обрезался» на середине (например, 01.01.01.000 без lookahead
    # матчился бы как 01.01.01.00).
    CODE_RE: re.Pattern[str] = re.compile(
        r"(?:из\s+)?(\d{2}\.\d{2}(?:\.\d{2}){0,2}(?:\.\d{3})?)(?!\d)"
    )

    # Служебные слова, которые не должны попадать в начало наименования
    _LEADING_NOISE_RE: re.Pattern[str] = re.compile(
        r"^(?:из|код\s+окрб|окрб|приложение|постановление|№|#)*\s*", re.IGNORECASE
    )

    def __init__(self, db: DatabaseManager) -> None:
        self.db: DatabaseManager = db

    # -- Публичный API ------------------------------------------------------
    def parse_pdf(self, pdf_path: str, flag_column: str) -> int:
        """Распарсить PDF и записать результаты в БД.

        :param pdf_path: путь к PDF-файлу.
        :param flag_column: столбец-флаг документа (in_decree_80 / in_168_appN).
        :return: количество найденных (обработанных) кодов.
        """
        if fitz is None:
            raise RuntimeError(
                "Модуль PyMuPDF (fitz) не установлен. Установите: pip install PyMuPDF"
            )
        if flag_column not in DOCUMENT_FLAGS:
            raise ValueError(f"Неизвестный флаг документа: {flag_column}")
        if not os.path.isfile(pdf_path):
            raise FileNotFoundError(f"Файл не найден: {pdf_path}")

        try:
            text = self._extract_text(pdf_path)
        except Exception as e:
            raise RuntimeError(f"Ошибка чтения PDF '{pdf_path}': {e}") from e

        items = self.extract_items(text)
        imported: int = 0
        for code, name in items:
            try:
                existing = self.db.get(code)
                if existing is None:
                    self.db.upsert(OkrbRecord(okrb_code=code, product_name=name,
                                             **{flag_column: 1}))
                else:
                    setattr(existing, flag_column, 1)
                    if name and not existing.product_name:
                        existing.product_name = name
                    self.db.upsert(existing)
                imported += 1
            except (RuntimeError, ValueError) as e:
                # отдельную «битую» запись не считаем фатальной ошибкой импорта
                print(f"[PDFParser] пропущен код {code}: {e}", file=sys.stderr)
        return imported

    @classmethod
    def extract_items(cls, text: str) -> List[Tuple[str, str]]:
        """Вернуть список пар (код, наименование) из плоского текста."""
        results: List[Tuple[str, str]] = []
        matches = list(cls.CODE_RE.finditer(text))
        for i, m in enumerate(matches):
            code: str = m.group(1)
            start: int = m.end()
            end: int = matches[i + 1].start() if i + 1 < len(matches) else len(text)

            # Наименование — текст до следующего кода, но не дальше конца
            # строки/абзаца, в котором находится текущий код.
            tail: str = text[start:end]
            line_end: int = cls._find_line_end(text, start)
            if line_end != -1 and line_end <= end:
                tail = text[start:line_end]

            name: str = cls._clean_name(tail)
            results.append((code, name))
        return results

    # -- Вспомогательные методы ---------------------------------------------
    @staticmethod
    def _extract_text(pdf_path: str) -> str:
        doc = fitz.open(pdf_path)
        try:
            chunks: List[str] = []
            for page in doc:
                chunks.append(page.get_text("text"))
            return "\n".join(chunks)
        finally:
            doc.close()

    @staticmethod
    def _find_line_end(text: str, pos: int) -> int:
        """Индекс первого перевода строки после pos (или -1)."""
        nl_unix: int = text.find("\n", pos)
        nl_win: int = text.find("\r", pos)
        candidates = [c for c in (nl_unix, nl_win) if c != -1]
        return min(candidates) if candidates else -1

    @classmethod
    def _clean_name(cls, raw: str) -> str:
        """Очистка извлечённого наименования: шум, лишние пробелы, пунктуация."""
        s: str = re.sub(r"\s+", " ", raw).strip()
        s = cls._LEADING_NOISE_RE.sub("", s)
        s = s.strip(" \t\r\n.,;:-–—(|)]}'\"")
        # обрезать случайный остаток следующего кода в конце
        s = re.sub(r"\d{2}\.\d{2}[\d.]*\s*$", "", s).strip()
        return s


def detect_flag_from_filename(filename: str) -> Optional[str]:
    """Маппинг имени файла на столбец-флаг.

    Правила: содержит '80' -> in_decree_80;
             содержит '168' и '1' -> in_168_app1 (аналогично 2 и 3).
    """
    base: str = os.path.basename(filename).lower()
    digits = re.findall(r"\d+", base)
    has_168 = "168" in digits
    has_80 = "80" in digits

    if has_168:
        # приложения: ищем номер приложения (кроме самого 168)
        for app_num, col in (("1", "in_168_app1"), ("2", "in_168_app2"), ("3", "in_168_app3")):
            rest = [d for d in digits if d != "168"]
            if app_num in rest:
                return col
        return None
    if has_80:
        return "in_decree_80"
    return None


# ---------------------------------------------------------------------------
# Диалог добавления / редактирования
# ---------------------------------------------------------------------------
class AddEditDialog(QDialog):
    """Диалог ввода/правки одной записи каталога ОКРБ."""

    def __init__(self, parent: Optional[QWidget] = None,
                 record: Optional[OkrbRecord] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Редактировать запись" if record else "Добавить запись")
        self.setModal(True)
        self.setMinimumWidth(480)

        form = QVBoxLayout(self)

        # Код ОКРБ
        code_row = QHBoxLayout()
        code_row.addWidget(QLabel("Код ОКРБ:"))
        self.edit_code: QLineEdit = QLineEdit()
        self.edit_code.setPlaceholderText("Например, 01.01.01.000")
        code_row.addWidget(self.edit_code)
        form.addLayout(code_row)

        # Наименование
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Наименование:"))
        self.edit_name: QLineEdit = QLineEdit()
        name_row.addWidget(self.edit_name)
        form.addLayout(name_row)

        # Флаги документов
        form.addWidget(QLabel("Куда включен:"))
        flags_box = QHBoxLayout()
        self.chk_80: QCheckBox = QCheckBox("Пост. 80")
        self.chk_168_1: QCheckBox = QCheckBox("Пост. 168, Прил. 1")
        self.chk_168_2: QCheckBox = QCheckBox("Пост. 168, Прил. 2")
        self.chk_168_3: QCheckBox = QCheckBox("Пост. 168, Прил. 3")
        for chk in (self.chk_80, self.chk_168_1, self.chk_168_2, self.chk_168_3):
            flags_box.addWidget(chk)
        form.addLayout(flags_box)

        # Кнопки
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        form.addWidget(buttons)

        if record is not None:
            self.edit_code.setText(record.okrb_code)
            self.edit_name.setText(record.product_name)
            self.chk_80.setChecked(bool(record.in_decree_80))
            self.chk_168_1.setChecked(bool(record.in_168_app1))
            self.chk_168_2.setChecked(bool(record.in_168_app2))
            self.chk_168_3.setChecked(bool(record.in_168_app3))

    def _on_accept(self) -> None:
        if not self.get_record():
            QMessageBox.warning(self, "Проверка ввода",
                                "Обязательно заполните код ОКРБ.")
            return
        self.accept()

    def get_record(self) -> Optional[OkrbRecord]:
        code: str = self.edit_code.text().strip()
        if not code:
            return None
        return OkrbRecord(
            okrb_code=code,
            product_name=self.edit_name.text().strip(),
            in_decree_80=int(self.chk_80.isChecked()),
            in_168_app1=int(self.chk_168_1.isChecked()),
            in_168_app2=int(self.chk_168_2.isChecked()),
            in_168_app3=int(self.chk_168_3.isChecked()),
        )


# ---------------------------------------------------------------------------
# Главное окно
# ---------------------------------------------------------------------------
class MainWindow(QWidget):
    """Главное окно приложения: поиск, таблица, кнопки управления."""

    def __init__(self, db: DatabaseManager) -> None:
        super().__init__()
        self.db: DatabaseManager = db
        self.parser: PDFParser = PDFParser(db)

        self.setWindowTitle("Каталог кодов ОКРБ")
        self.resize(1000, 650)
        self._build_ui()
        self.refresh_table()

    # -- UI ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # Строка поиска
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Код:"))
        self.search_code: QLineEdit = QLineEdit()
        self.search_code.setPlaceholderText("Поиск по коду ОКРБ…")
        self.search_code.setClearButtonEnabled(True)
        search_row.addWidget(self.search_code, stretch=1)

        search_row.addWidget(QLabel("Наименование:"))
        self.search_name: QLineEdit = QLineEdit()
        self.search_name.setPlaceholderText("Поиск по наименованию…")
        self.search_name.setClearButtonEnabled(True)
        search_row.addWidget(self.search_name, stretch=2)
        root.addLayout(search_row)

        # Мгновенный поиск: если заданы оба поля — логика AND,
        # поиск фильтрует всю БД (без ограничения в 100 записей).
        self.search_code.textChanged.connect(lambda _t: self.refresh_table(filtering=True))
        self.search_name.textChanged.connect(lambda _t: self.refresh_table(filtering=True))

        # Таблица
        self.table: QTableWidget = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Код ОКРБ", "Наименование", "Куда включен"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.doubleClicked.connect(lambda _idx: self.on_edit())
        root.addWidget(self.table, stretch=1)

        # Панель кнопок
        btn_row = QHBoxLayout()
        self.btn_import: QPushButton = QPushButton("Импорт из PDF")
        self.btn_import.clicked.connect(self.on_import_pdf)
        self.btn_add: QPushButton = QPushButton("Добавить запись")
        self.btn_add.clicked.connect(self.on_add)
        self.btn_edit: QPushButton = QPushButton("Редактировать")
        self.btn_edit.clicked.connect(self.on_edit)
        self.btn_delete: QPushButton = QPushButton("Удалить")
        self.btn_delete.clicked.connect(self.on_delete)
        for b in (self.btn_import, self.btn_add, self.btn_edit, self.btn_delete):
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        self.lbl_status: QLabel = QLabel("")
        btn_row.addWidget(self.lbl_status)
        root.addLayout(btn_row)

    # -- Данные --------------------------------------------------------------
    def refresh_table(self, filtering: bool = False) -> None:
        """Перезаполнить таблицу.

        При отсутствии активного поиска показываются первые PAGE_SIZE записей
        (быстродействие); при активном поиске фильтруется вся БД.
        """
        code_q: str = self.search_code.text()
        name_q: str = self.search_name.text()
        is_searching: bool = filtering or bool(code_q.strip() or name_q.strip())
        limit: Optional[int] = None if is_searching else PAGE_SIZE

        try:
            records = self.db.search(code_q, name_q, limit=limit)
            total = self.db.count_all()
        except RuntimeError as e:
            QMessageBox.critical(self, "Ошибка базы данных", str(e))
            return

        self.table.setRowCount(0)
        bold = QFont()
        bold.setBold(True)
        for rec in records:
            row: int = self.table.rowCount()
            self.table.insertRow(row)
            it_code = QTableWidgetItem(rec.okrb_code)
            it_code.setData(Qt.ItemDataRole.UserRole, rec.okrb_code)
            self.table.setItem(row, 0, it_code)
            self.table.setItem(row, 1, QTableWidgetItem(rec.product_name))
            inc_item = QTableWidgetItem(rec.inclusion_text())
            if not (rec.in_decree_80 or rec.in_168_app1 or
                    rec.in_168_app2 or rec.in_168_app3):
                inc_item.setFont(bold)
            self.table.setItem(row, 2, inc_item)

        shown = len(records)
        if is_searching:
            self.lbl_status.setText(f"Найдено: {shown}")
        else:
            more = total - shown
            suffix = f" (показаны первые {PAGE_SIZE}, всего в базе: {total})" if more > 0 \
                else f" (всего: {total})"
            self.lbl_status.setText(f"Записей: {shown}{suffix}")

    def selected_code(self) -> Optional[str]:
        row: int = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    # -- Слоты кнопок ----------------------------------------------------------
    def on_import_pdf(self) -> None:
        if fitz is None:
            QMessageBox.critical(
                self, "Ошибка",
                "Не найден модуль PyMuPDF (fitz).\nУстановите его: pip install PyMuPDF")
            return

        files, _ = QFileDialog.getOpenFileNames(
            self, "Выберите PDF-файлы с перечнями ОКРБ", "",
            "PDF-файлы (*.pdf)")
        if not files:
            return

        report: List[str] = []
        errors: List[str] = []
        for path in files:
            flag = detect_flag_from_filename(path)
            if flag is None:
                errors.append(
                    f"«{os.path.basename(path)}»: не удалось определить документ "
                    "по имени (ожидаются '80' или '168' + номер приложения 1/2/3).")
                continue
            try:
                n = self.parser.parse_pdf(path, flag)
                report.append(f"«{os.path.basename(path)}» → {flag}: {n} кодов.")
            except (RuntimeError, FileNotFoundError, ValueError) as e:
                errors.append(str(e))

        self.refresh_table()

        if report:
            QMessageBox.information(self, "Импорт завершён", "\n".join(report))
        if errors:
            QMessageBox.warning(self, "Импорт с ошибками", "\n".join(errors))

    def on_add(self) -> None:
        dlg = AddEditDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            rec = dlg.get_record()
            if rec is None:
                return
            try:
                if self.db.get(rec.okrb_code) is not None:
                    if QMessageBox.question(
                            self, "Запись существует",
                            f"Код {rec.okrb_code} уже есть в базе. Перезаписать?") \
                            != QMessageBox.StandardButton.Yes:
                        return
                self.db.upsert(rec)
                self.refresh_table()
            except RuntimeError as e:
                QMessageBox.critical(self, "Ошибка", str(e))

    def on_edit(self) -> None:
        code = self.selected_code()
        if code is None:
            QMessageBox.information(self, "Редактирование",
                                    "Сначала выберите строку в таблице.")
            return
        try:
            rec = self.db.get(code)
        except RuntimeError as e:
            QMessageBox.critical(self, "Ошибка", str(e))
            return
        if rec is None:
            QMessageBox.warning(self, "Ошибка", f"Запись {code} не найдена в БД.")
            return

        dlg = AddEditDialog(self, record=rec)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_rec = dlg.get_record()
            if new_rec is None:
                return
            try:
                if new_rec.okrb_code != code:
                    self.db.delete(code)
                self.db.upsert(new_rec)
                self.refresh_table()
            except RuntimeError as e:
                QMessageBox.critical(self, "Ошибка", str(e))

    def on_delete(self) -> None:
        code = self.selected_code()
        if code is None:
            QMessageBox.information(self, "Удаление",
                                    "Сначала выберите строку в таблице.")
            return
        answer = QMessageBox.question(
            self, "Подтверждение удаления",
            f"Удалить запись {code} из базы?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            if self.db.delete(code):
                self.refresh_table()
            else:
                QMessageBox.warning(self, "Удаление", "Запись не найдена.")
        except RuntimeError as e:
            QMessageBox.critical(self, "Ошибка", str(e))

    # -- Закрытие --------------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        self.db.close()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------
def main() -> int:
    try:
        db = DatabaseManager(DB_FILENAME)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1

    app = QApplication(sys.argv)
    app.setApplicationName("ОКРБ Catalog")
    try:
        win = MainWindow(db)
        win.show()
        return app.exec()
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
