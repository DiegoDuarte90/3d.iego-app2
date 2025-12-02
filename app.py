from flask import Flask, jsonify, render_template, request, send_file, redirect, url_for
from collections import OrderedDict
import sqlite3
from pathlib import Path
from datetime import datetime
import io
from pdfgen import build_entrega_pdf
import re

# Ruta a la base de datos
DB_PATH = Path("3d_iego.db")

# Flask config
app = Flask(__name__, template_folder="templates", static_folder="static")


# ---------------------------
# UTILIDADES BASE DE DATOS
# ---------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def obtener_productos():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            id,
            nombre,
            tipo_pieza,
            subtipo,
            stock,
            precio,
            precio_revendedor,
            notas
        FROM productos
        WHERE activo = 1
        ORDER BY nombre;
    """)
    filas = cur.fetchall()
    conn.close()
    return [dict(f) for f in filas]


def obtener_revendedores():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            id,
            nombre,
            contacto,
            notas,
            saldo_inicial,
            created_at,
            updated_at
        FROM revendedores
        WHERE activo = 1
        ORDER BY nombre;
    """)
    filas = cur.fetchall()
    conn.close()
    return [dict(f) for f in filas]


# ---------------------------
# UTILIDAD: PDF DE ENTREGA
# ---------------------------

def _formatear_fecha_ddmmyyyy(fecha_raw: str) -> str:
    """
    Convierte 'YYYY-MM-DD' en 'DD-MM-YYYY'.
    Si falla, devuelve la fecha original.
    """
    if not fecha_raw:
        return ""
    try:
        y, m, d = fecha_raw.split("-")
        return f"{d}-{m}-{y}"
    except Exception:
        return fecha_raw


# ---------------------------
# RUTAS FRONTEND
# ---------------------------

@app.route("/")
def dashboard():
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # -------- rango mes actual --------
    hoy = datetime.now().date()
    inicio_mes = hoy.replace(day=1)
    if inicio_mes.month == 12:
        fin_mes = inicio_mes.replace(year=inicio_mes.year + 1, month=1)
    else:
        fin_mes = inicio_mes.replace(month=inicio_mes.month + 1)

    mes_clave_actual = inicio_mes.strftime("%Y-%m")

    # -------- PAGOS DEL MES --------
    cur.execute("""
        SELECT monto, costo
        FROM pagos
        WHERE mes_clave = ?
    """, (mes_clave_actual,))
    pagos = cur.fetchall()

    total_montos = sum(p["monto"] for p in pagos) if pagos else 0.0
    total_costos = sum(p["costo"] for p in pagos) if pagos else 0.0

    # -------- GASTOS DEL MES (sin filamento) --------
    try:
        cur.execute("""
            SELECT COALESCE(SUM(monto), 0) AS total_gastos
            FROM gastos
            WHERE mes_clave = ?
              AND tipo = 'gasto'
              AND IFNULL(es_filamento, 0) = 0
        """, (mes_clave_actual,))
        row_g = cur.fetchone()
        total_gastos = float(row_g["total_gastos"]) if row_g else 0.0
    except sqlite3.OperationalError:
        # si todavía no existe la tabla/columna, tomamos 0
        total_gastos = 0.0

    # 1) GANANCIA DEL MES (BRUTA REAL)
    ganancia_mes = total_montos - total_costos - total_gastos

    # 2) GANANCIA INDIVIDUAL (MITAD)
    ganancia_individual_mes = ganancia_mes / 2.0

    # -------- Pieza más vendida --------
    cur.execute("""
        SELECT ei.nombre_pieza AS pieza, SUM(ei.cantidad) AS total_cant
        FROM entrega_items ei
        JOIN entregas e ON ei.entrega_id = e.id
        WHERE e.fecha >= ? AND e.fecha < ?
        GROUP BY ei.nombre_pieza
        ORDER BY total_cant DESC
        LIMIT 1
    """, (inicio_mes.isoformat(), fin_mes.isoformat()))
    row = cur.fetchone()
    pieza_mas_vendida = row["pieza"] if row else None

    # -------- Revendedor top --------
    cur.execute("""
        SELECT r.nombre AS revendedor, SUM(e.total) AS total_ventas
        FROM entregas e
        JOIN revendedores r ON e.revendedor_id = r.id
        WHERE e.tipo_cliente = 'revendedor'
          AND e.fecha >= ? AND e.fecha < ?
        GROUP BY r.id
        ORDER BY total_ventas DESC
        LIMIT 1
    """, (inicio_mes.isoformat(), fin_mes.isoformat()))
    row = cur.fetchone()
    revendedor_top = row["revendedor"] if row else None

    conn.close()

    return render_template(
        "dashboard.html",
        ganancia_mes=ganancia_mes,
        ganancia_individual_mes=ganancia_individual_mes,
        pieza_mas_vendida=pieza_mas_vendida,
        revendedor_top=revendedor_top,
    )


@app.route("/stock")
def pagina_stock():
    return render_template("stock.html")


@app.route("/revendedores")
def pagina_revendedores():
    return render_template("revendedores.html")


@app.route("/api/revendedores/borrar", methods=["POST"])
def api_borrar_revendedor_completo():
    data = request.get_json(force=True) or {}
    rev_id = data.get("id")

    if not rev_id:
        return jsonify(ok=False, error="Falta ID del revendedor"), 200

    try:
        rev_id = int(rev_id)
    except Exception:
        return jsonify(ok=False, error="ID inválido"), 200

    conn = get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Verificar entregas asociadas
    cur.execute("SELECT COUNT(*) AS c FROM entregas WHERE revendedor_id = ?", (rev_id,))
    tiene_entregas = cur.fetchone()["c"]

    # Verificar pagos asociados
    cur.execute("SELECT COUNT(*) AS c FROM pagos WHERE revendedor_id = ?", (rev_id,))
    tiene_pagos = cur.fetchone()["c"]

    if tiene_entregas > 0 or tiene_pagos > 0:
        conn.close()
        return jsonify(ok=False, error="No se puede borrar: tiene entregas o pagos asociados."), 200

    # Borrado lógico
    cur.execute("""
        UPDATE revendedores
        SET activo = 0, updated_at = datetime('now')
        WHERE id = ?
    """, (rev_id,))

    conn.commit()
    filas = cur.rowcount
    conn.close()

    if filas == 0:
        return jsonify(ok=False, error="Revendedor no encontrado"), 200

    return jsonify(ok=True), 200


@app.route("/entregas")
def pagina_entregas():
    revendedores = obtener_revendedores()
    productos = obtener_productos()

    conn = get_conn()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT
                id,
                fecha,
                tipo_cliente,
                cliente_nombre,
                revendedor_id,
                cantidad_total,
                total
            FROM entregas
            ORDER BY fecha DESC, id DESC
            LIMIT 25;
        """)
        filas = cur.fetchall()
        historial_entregas = [dict(f) for f in filas]
    except sqlite3.OperationalError:
        historial_entregas = []
    finally:
        conn.close()

    return render_template(
        "entregas.html",
        revendedores=revendedores,
        productos=productos,
        piezas_en_stock=productos,
        historial_entregas=historial_entregas
    )


# ---------------------------
# CUENTAS (PAGOS + GASTOS)
# ---------------------------

@app.route("/cuentas", methods=["GET", "POST"])
def cuentas():
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # --- aseguramos tablas necesarias ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pagos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha DATE NOT NULL,
            tipo_cliente TEXT NOT NULL,              -- 'revendedor' / 'particular'
            revendedor_id INTEGER,
            nombre_particular TEXT,
            descripcion TEXT,
            categoria_precio TEXT NOT NULL,          -- 'normal' / 'revendedor'
            monto REAL NOT NULL,
            division INTEGER NOT NULL,
            costo REAL NOT NULL,
            ganancia REAL NOT NULL,
            ganancia_individual REAL NOT NULL,
            mes_clave TEXT NOT NULL                  -- 'YYYY-MM'
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS gastos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha DATE NOT NULL,
            tipo TEXT NOT NULL,                      -- 'gasto' / 'pago_ayudante'
            descripcion TEXT,
            monto REAL NOT NULL,
            mes_clave TEXT NOT NULL
        );
    """)

    # columna extra para filamento
    try:
        cur.execute("ALTER TABLE gastos ADD COLUMN es_filamento INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    # ------------------ POST ------------------
    if request.method == "POST":
        form_type = request.form.get("form_type", "pago")

        # ------------- NUEVO PAGO -------------
        if form_type == "pago":
            fecha_str = request.form.get("fecha") or datetime.now().strftime("%Y-%m-%d")
            revendedor_id = request.form.get("revendedor_id") or None
            nombre_particular = (request.form.get("nombre_particular") or "").strip() or None
            descripcion = (request.form.get("descripcion") or "").strip()

            if revendedor_id:
                tipo_cliente = "revendedor"
            else:
                tipo_cliente = "particular"

            categoria_default = "revendedor" if tipo_cliente == "revendedor" else "normal"

            monto_total = float(request.form.get("monto") or 0)
            division_default = int(request.form.get("division") or 1)
            dividir = request.form.get("dividir")
            mes_clave = fecha_str[:7]

            def preparar_registro(monto_unit, division_unit, categoria_unit):
                if not monto_unit or monto_unit <= 0:
                    return None
                costo = monto_unit / division_unit
                ganancia = monto_unit - costo
                ganancia_individual = ganancia / 2.0
                return (
                    fecha_str,
                    tipo_cliente,
                    int(revendedor_id) if (tipo_cliente == "revendedor" and revendedor_id) else None,
                    nombre_particular if tipo_cliente == "particular" else None,
                    descripcion,
                    categoria_unit,
                    monto_unit,
                    division_unit,
                    costo,
                    ganancia,
                    ganancia_individual,
                    mes_clave,
                )

            registros = []

            if dividir == "on":
                monto1 = float(request.form.get("monto1") or 0)
                division1 = int(request.form.get("division1") or division_default)
                categoria1 = request.form.get("categoria1") or categoria_default

                monto2 = float(request.form.get("monto2") or 0)
                division2 = int(request.form.get("division2") or division_default)
                categoria2 = request.form.get("categoria2") or categoria_default

                reg1 = preparar_registro(monto1, division1, categoria1)
                reg2 = preparar_registro(monto2, division2, categoria2)
                if reg1:
                    registros.append(reg1)
                if reg2:
                    registros.append(reg2)
            else:
                reg = preparar_registro(monto_total, division_default, categoria_default)
                if reg:
                    registros.append(reg)

            for r in registros:
                cur.execute("""
                    INSERT INTO pagos (
                        fecha, tipo_cliente, revendedor_id, nombre_particular,
                        descripcion, categoria_precio, monto, division,
                        costo, ganancia, ganancia_individual, mes_clave
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, r)
            conn.commit()

        # ------------- NUEVO GASTO O PAGO AYUDANTE -------------
        elif form_type == "gasto":
            fecha_g = request.form.get("fecha_gasto") or datetime.now().strftime("%Y-%m-%d")
            descripcion_g = (request.form.get("descripcion_gasto") or "").strip()
            monto_g = float(request.form.get("monto_gasto") or 0)
            tipo_g = request.form.get("tipo_gasto") or "gasto"   # 'gasto' / 'pago_ayudante'
            mes_clave_g = fecha_g[:7]
            es_filamento = 1 if request.form.get("es_filamento") in ("1", "on") else 0

            if monto_g > 0:
                cur.execute("""
                    INSERT INTO gastos (fecha, tipo, descripcion, monto, mes_clave, es_filamento)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (fecha_g, tipo_g, descripcion_g, monto_g, mes_clave_g, es_filamento))
                conn.commit()

        # ------------- EDITAR GASTO NORMAL -------------
        elif form_type == "gasto_edit":
            gasto_id = request.form.get("gasto_id")
            fecha_g = request.form.get("fecha_gasto") or datetime.now().strftime("%Y-%m-%d")
            descripcion_g = (request.form.get("descripcion_gasto") or "").strip()
            monto_g = float(request.form.get("monto_gasto") or 0)
            es_filamento = 1 if request.form.get("es_filamento") in ("1", "on") else 0
            mes_clave_g = fecha_g[:7]

            if gasto_id and monto_g > 0:
                cur.execute("""
                    UPDATE gastos
                    SET fecha = ?, descripcion = ?, monto = ?, mes_clave = ?, es_filamento = ?
                    WHERE id = ? AND tipo = 'gasto'
                """, (fecha_g, descripcion_g, monto_g, mes_clave_g, es_filamento, gasto_id))
                conn.commit()

        # ------------- EDITAR PAGO AYUDANTE -------------
        elif form_type == "pago_ayudante_edit":
            gasto_id = request.form.get("gasto_id")
            fecha_g = request.form.get("fecha_gasto") or datetime.now().strftime("%Y-%m-%d")
            descripcion_g = (request.form.get("descripcion_gasto") or "").strip()
            monto_g = float(request.form.get("monto_gasto") or 0)
            mes_clave_g = fecha_g[:7]

            if gasto_id and monto_g > 0:
                cur.execute("""
                    UPDATE gastos
                    SET fecha = ?, descripcion = ?, monto = ?, mes_clave = ?
                    WHERE id = ? AND tipo = 'pago_ayudante'
                """, (fecha_g, descripcion_g, monto_g, mes_clave_g, gasto_id))
                conn.commit()

    # ------------------ SELECTOR DE MESES ------------------
    cur.execute("""
        SELECT mes_clave FROM pagos
        UNION
        SELECT mes_clave FROM gastos
        ORDER BY mes_clave DESC
    """)
    filas_meses = cur.fetchall()
    meses_disponibles = [f["mes_clave"] for f in filas_meses]

    mes_param = request.args.get("mes")
    if mes_param and mes_param in meses_disponibles:
        mes_seleccionado = mes_param
    else:
        mes_seleccionado = meses_disponibles[0] if meses_disponibles else None

    meses = OrderedDict()

    # ------------------ PAGOS DEL MES ------------------
    if mes_seleccionado:
        cur.execute("""
            SELECT
                p.*,
                r.nombre AS revendedor_nombre
            FROM pagos p
            LEFT JOIN revendedores r ON p.revendedor_id = r.id
            WHERE p.mes_clave = ?
            ORDER BY p.fecha DESC, p.id DESC
        """, (mes_seleccionado,))
        rows = cur.fetchall()

        grupos = {}
        totales = {
            "monto": 0.0,
            "costo": 0.0,
            "ganancia": 0.0,
            "ganancia_individual": 0.0,
        }

        for row in rows:
            row = dict(row)
            clave = (
                row["fecha"],
                row["tipo_cliente"],
                row["revendedor_id"],
                row["nombre_particular"],
                row["descripcion"],
            )

            if clave not in grupos:
                grupos[clave] = {
                    "ids": [row["id"]],
                    "fecha": row["fecha"],
                    "tipo_cliente": row["tipo_cliente"],
                    "revendedor_id": row["revendedor_id"],
                    "revendedor_nombre": row.get("revendedor_nombre"),
                    "nombre_particular": row.get("nombre_particular"),
                    "descripcion": row.get("descripcion"),
                    "monto": float(row["monto"] or 0),
                    "costo": float(row["costo"] or 0),
                    "ganancia": float(row["ganancia"] or 0),
                    "ganancia_individual": float(row["ganancia_individual"] or 0),
                    "detalles": [
                        {
                            "monto": float(row["monto"] or 0),
                            "division": int(row["division"] or 1),
                            "categoria_precio": row["categoria_precio"],
                        }
                    ],
                }
            else:
                g = grupos[clave]
                g["ids"].append(row["id"])
                g["monto"] += float(row["monto"] or 0)
                g["costo"] += float(row["costo"] or 0)
                g["ganancia"] += float(row["ganancia"] or 0)
                g["ganancia_individual"] += float(row["ganancia_individual"] or 0)
                g["detalles"].append(
                    {
                        "monto": float(row["monto"] or 0),
                        "division": int(row["division"] or 1),
                        "categoria_precio": row["categoria_precio"],
                    }
                )

            totales["monto"] += float(row["monto"] or 0)
            totales["costo"] += float(row["costo"] or 0)
            totales["ganancia"] += float(row["ganancia"] or 0)
            totales["ganancia_individual"] += float(row["ganancia_individual"] or 0)

        items = list(grupos.values())
        items.sort(key=lambda g: (g["fecha"], max(g["ids"])), reverse=True)

        meses[mes_seleccionado] = {
            "items": items,
            "totales": totales,
        }

    # ------------------ GASTOS + RESUMEN ------------------
    resumen_mes = None
    gastos_mes_list = []
    pagos_ayudante_list = []

    if mes_seleccionado:
        # Totales de gastos (sin filamento) y pagos ayudante
        cur.execute("""
            SELECT
              COALESCE(SUM(CASE
                             WHEN tipo = 'gasto'
                              AND IFNULL(es_filamento, 0) = 0
                             THEN monto
                             ELSE 0
                           END), 0) AS total_gastos,
              COALESCE(SUM(CASE
                             WHEN tipo = 'pago_ayudante'
                             THEN monto
                             ELSE 0
                           END), 0) AS total_pagos_ayudante
            FROM gastos
            WHERE mes_clave = ?
        """, (mes_seleccionado,))
        row_g = cur.fetchone()
        total_gastos = float(row_g["total_gastos"] or 0) if row_g else 0.0
        total_pagos_ayudante = float(row_g["total_pagos_ayudante"] or 0) if row_g else 0.0

        if mes_seleccionado in meses:
            gi_bruta = meses[mes_seleccionado]["totales"]["ganancia_individual"]
        else:
            gi_bruta = 0.0

        # gi_bruta ya es (∑montos - ∑costos)/2
        # gi_neta = gi_bruta - (gastos_sin_filamento / 2)
        gi_neta = gi_bruta - total_gastos / 2.0
        ayudante_pendiente = gi_neta - total_pagos_ayudante

        resumen_mes = {
            "gastos_mes": total_gastos,
            "pagado_ayudante": total_pagos_ayudante,
            "gi_bruta": gi_bruta,
            "gi_neta": gi_neta,
            "ayudante": ayudante_pendiente,
        }

        # SOLO gastos "normales" (los listamos todos, incluyendo filamento, para verlos)
        cur.execute("""
            SELECT id, fecha, descripcion, tipo, monto, es_filamento
            FROM gastos
            WHERE mes_clave = ? AND tipo = 'gasto'
            ORDER BY fecha DESC, id DESC
        """, (mes_seleccionado,))
        gastos_mes_list = [dict(r) for r in cur.fetchall()]

        # Lista de pagos al ayudante (para el popup)
        cur.execute("""
            SELECT id, fecha, descripcion, monto
            FROM gastos
            WHERE mes_clave = ? AND tipo = 'pago_ayudante'
            ORDER BY fecha DESC, id DESC
        """, (mes_seleccionado,))
        pagos_ayudante_list = [dict(r) for r in cur.fetchall()]

    # Combo de revendedores
    cur.execute("SELECT id, nombre FROM revendedores WHERE activo = 1 ORDER BY nombre;")
    revendedores = cur.fetchall()

    # PARA FILAMENTO = suma de costos del mes (según tu definición)
    para_filamento_mes = 0.0
    if mes_seleccionado and mes_seleccionado in meses:
        para_filamento_mes = float(meses[mes_seleccionado]["totales"]["costo"] or 0)

    conn.close()
    hoy = datetime.now().strftime("%Y-%m-%d")

    return render_template(
        "cuentas.html",
        meses=meses,
        revendedores=revendedores,
        meses_disponibles=meses_disponibles,
        mes_seleccionado=mes_seleccionado,
        resumen_mes=resumen_mes,
        gastos_mes_list=gastos_mes_list,
        pagos_ayudante_list=pagos_ayudante_list,
        hoy=hoy,
        para_filamento_mes=para_filamento_mes,
    )


# ---------------------------
# API ENTREGAS (JSON + creación)
# ---------------------------

@app.route("/api/entregas/<int:entrega_id>", methods=["GET"])
def api_entrega_detalle(entrega_id):
    """
    Detalle de entrega en JSON para el popup (revendedores.html)
    """
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Cabecera
    cur.execute("""
        SELECT
            id,
            fecha,
            tipo_cliente,
            cliente_nombre,
            cantidad_total,
            total
        FROM entregas
        WHERE id = ?
    """, (entrega_id,))
    entrega = cur.fetchone()

    if not entrega:
        conn.close()
        return jsonify({"ok": False, "error": "Entrega no encontrada"}), 404

    # Ítems
    cur.execute("""
        SELECT
            id,
            nombre_pieza,
            cantidad,
            precio_unitario,
            total
        FROM entrega_items
        WHERE entrega_id = ?
        ORDER BY id
    """, (entrega_id,))
    items = cur.fetchall()
    conn.close()

    return jsonify({
        "ok": True,
        "entrega": dict(entrega),
        "items": [dict(i) for i in items],
    })


@app.route("/api/entregas", methods=["POST"])
def api_crear_entrega():
    data = request.get_json(force=True) or {}

    tipo_cliente = (data.get("tipo_cliente") or "").strip()
    revendedor_id = data.get("revendedor_id")
    cliente_nombre = (data.get("cliente_nombre") or "").strip()
    fecha = (data.get("fecha") or "").strip()
    piezas = data.get("piezas") or []
    cantidad_total = int(data.get("cantidad_total") or 0)
    total = float(data.get("total") or 0)

    if tipo_cliente not in ("revendedor", "particular"):
        return jsonify({"error": "Tipo de cliente inválido"}), 400
    if not cliente_nombre:
        return jsonify({"error": "Falta nombre del cliente"}), 400
    if not fecha:
        return jsonify({"error": "Falta la fecha de entrega"}), 400
    if not piezas:
        return jsonify({"error": "La entrega no tiene piezas"}), 400

    conn = get_conn()
    cur = conn.cursor()

    try:
        # CABECERA
        cur.execute("""
            INSERT INTO entregas (
                fecha, tipo_cliente, revendedor_id, cliente_nombre,
                cantidad_total, total
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (fecha, tipo_cliente, revendedor_id, cliente_nombre, cantidad_total, total))

        entrega_id = cur.lastrowid

        # ITEMS + descuento de stock
        for item in piezas:
            producto_id = item.get("producto_id")
            nombre_pieza = item.get("nombre_pieza")
            cantidad = int(item.get("cantidad") or 0)
            precio_unit = float(item.get("precio_unitario") or 0)
            total_item = float(item.get("total") or (cantidad * precio_unit))

            if not nombre_pieza or cantidad <= 0:
                continue

            cur.execute("""
                INSERT INTO entrega_items (
                    entrega_id, producto_id, nombre_pieza,
                    cantidad, precio_unitario, total
                )
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (entrega_id, producto_id, nombre_pieza, cantidad, precio_unit, total_item))

            if producto_id:
                cur.execute("SELECT stock FROM productos WHERE id = ?", (producto_id,))
                fila = cur.fetchone()
                if fila:
                    nuevo_stock = max(0, (fila["stock"] or 0) - cantidad)
                    cur.execute("""
                        UPDATE productos
                        SET stock = ?, updated_at = datetime('now')
                        WHERE id = ?
                    """, (nuevo_stock, producto_id))

        conn.commit()

    except Exception as e:
        conn.rollback()
        return jsonify({"error": f"No se pudo guardar la entrega: {e}"}), 500
    finally:
        conn.close()

    return jsonify({"ok": True, "entrega_id": entrega_id})


# ---------------------------
# API PRODUCTOS
# ---------------------------

@app.route("/api/productos", methods=["GET"])
def api_productos():
    return jsonify(obtener_productos())


@app.route("/api/productos", methods=["POST"])
def api_crear_producto():
    data = request.get_json(force=True) or {}

    nombre = (data.get("nombre") or "").strip()
    tipo_pieza = (data.get("tipo_pieza") or "").strip()
    subtipo = (data.get("subtipo") or "").strip()
    notas = (data.get("notas") or "").strip()

    if not nombre:
        return jsonify({"error": "El nombre es obligatorio"}), 400

    try:
        stock = int(data.get("stock") or 0)
        precio = float(data.get("precio") or 0)
        precio_rev = float(data.get("precio_revendedor") or 0)
    except Exception:
        return jsonify({"error": "Stock y precios deben ser numéricos"}), 400

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO productos (
            nombre,
            tipo_pieza,
            subtipo,
            stock,
            precio,
            precio_revendedor,
            notas,
            activo,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, datetime('now'), datetime('now'))
    """,
        (nombre, tipo_pieza, subtipo, stock, precio, precio_rev, notas))

    conn.commit()
    nuevo_id = cur.lastrowid
    conn.close()

    return jsonify({"ok": True, "id": nuevo_id})


@app.route("/api/productos/<int:producto_id>", methods=["PUT"])
def api_actualizar_producto(producto_id):
    data = request.get_json(force=True) or {}

    campos = ["nombre", "tipo_pieza", "subtipo", "stock",
              "precio", "precio_revendedor", "notas"]

    if not all(k in data for k in campos):
        return jsonify({"error": "Faltan campos"}), 400

    try:
        stock = int(data["stock"])
        precio = float(data["precio"])
        precio_rev = float(data["precio_revendedor"])
    except Exception:
        return jsonify({"error": "Stock y precios deben ser numéricos"}), 400

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE productos
        SET nombre = ?, tipo_pieza = ?, subtipo = ?, stock = ?,
            precio = ?, precio_revendedor = ?, notas = ?, updated_at = datetime('now')
        WHERE id = ?
    """,
        (
            data["nombre"], data["tipo_pieza"], data["subtipo"], stock,
            precio, precio_rev, data["notas"], producto_id
        ))

    conn.commit()
    filas_afectadas = cur.rowcount
    conn.close()

    if filas_afectadas == 0:
        return jsonify({"error": "Producto no encontrado"}), 404

    return jsonify({"ok": True})


@app.route("/api/productos/<int:producto_id>", methods=["DELETE"])
def api_borrar_producto(producto_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE productos
        SET activo = 0,
            updated_at = datetime('now')
        WHERE id = ?
    """, (producto_id,))
    conn.commit()
    filas = cur.rowcount
    conn.close()

    if filas == 0:
        return jsonify({"error": "Producto no encontrado"}), 404

    return jsonify({"ok": True})


# ---------------------------
# API REVENDEDORES
# ---------------------------

@app.route("/api/revendedores", methods=["GET", "POST"])
def api_revendedores():
    """
    GET  -> lista de revendedores con saldo_actual calculado
    POST -> crea un nuevo revendedor
    """
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # ------------------- GET: LISTAR -------------------
    if request.method == "GET":
        # Traemos todos los revendedores activos
        cur.execute("""
            SELECT
                id,
                nombre,
                contacto,
                notas,
                saldo_inicial,
                created_at,
                updated_at
            FROM revendedores
            WHERE activo = 1
            ORDER BY nombre;
        """)
        filas = cur.fetchall()

        resultado = []

        for r in filas:
            rev_id = r["id"]
            saldo_base = float(r["saldo_inicial"] or 0)

            # Suma de entregas a este revendedor
            cur.execute("""
                SELECT COALESCE(SUM(total), 0) AS suma_entregas
                FROM entregas
                WHERE tipo_cliente = 'revendedor'
                  AND revendedor_id = ?
            """, (rev_id,))
            row_ent = cur.fetchone()
            suma_entregas = float(row_ent["suma_entregas"] or 0) if row_ent else 0.0

            # Suma de pagos de este revendedor
            cur.execute("""
                SELECT COALESCE(SUM(monto), 0) AS suma_pagos
                FROM pagos
                WHERE tipo_cliente = 'revendedor'
                  AND revendedor_id = ?
            """, (rev_id,))
            row_pag = cur.fetchone()
            suma_pagos = float(row_pag["suma_pagos"] or 0) if row_pag else 0.0

            # saldo real (positivo = nos debe, negativo = a favor)
            saldo_actual = saldo_base + suma_entregas - suma_pagos

            d = dict(r)
            d["saldo_actual"] = saldo_actual
            resultado.append(d)

        conn.close()
        return jsonify(resultado)

    # ------------------- POST: CREAR -------------------
    # si llegamos acá es porque method == "POST"
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        conn.close()
        return jsonify({"ok": False, "error": "JSON inválido"}), 200

    nombre = (data.get("nombre") or "").strip()
    if not nombre:
        conn.close()
        return jsonify({"ok": False, "error": "El nombre es obligatorio"}), 200

    contacto = (data.get("contacto") or "").strip()
    notas = (data.get("notas") or "").strip()

    try:
        saldo_inicial = float(data.get("saldo_inicial") or 0)
    except Exception:
        conn.close()
        return jsonify({"ok": False, "error": "Saldo inicial inválido"}), 200

    try:
        cur.execute("""
            INSERT INTO revendedores (
                nombre,
                contacto,
                notas,
                saldo_inicial,
                activo,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, 1, datetime('now'), datetime('now'))
        """, (nombre, contacto, notas, saldo_inicial))
        conn.commit()
        nuevo_id = cur.lastrowid
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"ok": False, "error": f"Error SQLite: {e}"}), 200

    conn.close()
    return jsonify({"ok": True, "id": nuevo_id}), 200



@app.route("/api/revendedores/<int:rev_id>", methods=["PUT"])
def api_actualizar_revendedor(rev_id):
    data = request.get_json(force=True) or {}

    nombre = (data.get("nombre") or "").strip()
    contacto = (data.get("contacto") or "").strip()
    notas = (data.get("notas") or "").strip()

    if not nombre:
        return jsonify({"error": "El nombre es obligatorio"}), 400

    try:
        saldo_inicial = float(data.get("saldo_inicial") or 0)
    except Exception:
        return jsonify({"error": "Saldo inicial inválido"}), 400

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        UPDATE revendedores
        SET nombre = ?, contacto = ?, notas = ?, saldo_inicial = ?, updated_at = datetime('now')
        WHERE id = ? AND activo = 1
    """, (nombre, contacto, notas, saldo_inicial, rev_id))

    conn.commit()
    filas = cur.rowcount
    conn.close()

    if filas == 0:
        return jsonify({"error": "Revendedor no encontrado"}), 404

    return jsonify({"ok": True})


@app.route("/api/revendedores/<int:rev_id>", methods=["DELETE"])
def api_borrar_revendedor(rev_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE revendedores
        SET activo = 0,
            updated_at = datetime('now')
        WHERE id = ?
    """, (rev_id,))
    conn.commit()
    filas = cur.rowcount
    conn.close()

    if filas == 0:
        return jsonify({"error": "Revendedor no encontrado"}), 404

    return jsonify({"ok": True})


@app.route("/api/revendedores/<int:rev_id>/movimientos", methods=["GET"])
def api_movimientos_revendedor(rev_id):
    """
    Movimientos (entregas + pagos) de un revendedor, con saldo acumulado.
    Entregas suman al saldo (te debe).
    Pagos restan al saldo.
    En la tabla mostramos los montos con signo "al revés":
      - entrega -> monto negativo
      - pago    -> monto positivo
    """
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # saldo inicial
    cur.execute("SELECT saldo_inicial FROM revendedores WHERE id = ?", (rev_id,))
    row_rev = cur.fetchone()
    if not row_rev:
        conn.close()
        return jsonify({"error": "Revendedor no encontrado"}), 404

    saldo = float(row_rev["saldo_inicial"] or 0)

    # Entregas
    cur.execute("""
        SELECT
            e.id,
            e.fecha,
            e.cantidad_total,
            e.total
        FROM entregas e
        WHERE e.tipo_cliente = 'revendedor'
          AND e.revendedor_id = ?
        ORDER BY e.fecha ASC, e.id ASC
    """, (rev_id,))
    entregas = cur.fetchall()

    # Pagos
    cur.execute("""
        SELECT
            p.id,
            p.fecha,
            p.descripcion,
            p.monto
        FROM pagos p
        WHERE p.tipo_cliente = 'revendedor'
          AND p.revendedor_id = ?
        ORDER BY p.fecha ASC, p.id ASC
    """, (rev_id,))
    pagos = cur.fetchall()

    conn.close()

    eventos = []

    for e in entregas:
        eventos.append({
            "id": e["id"],
            "tipo": "entrega",
            "fecha": e["fecha"],
            "descripcion": f"Entrega #{e['id']} · {e['cantidad_total']} piezas",
            "monto": float(e["total"] or 0),
        })

    for p in pagos:
        monto_pago = float(p["monto"] or 0)
        eventos.append({
            "id": p["id"],
            "tipo": "pago",
            "fecha": p["fecha"],
            "descripcion": (p["descripcion"] or f"Pago recibido #{p['id']}"),
            "monto": -abs(monto_pago),  # pagos restan al saldo
        })

    # Ordenar por fecha, luego tipo, luego id
    eventos.sort(key=lambda ev: (ev["fecha"], 0 if ev["tipo"] == "entrega" else 1, ev["id"]))

    movimientos = []
    for ev in eventos:
        # saldo real (positivo = nos debe, negativo = a favor)
        saldo += ev["monto"]

        # signo "invertido" para mostrar:
        if ev["tipo"] == "entrega":
            total_visible = -ev["monto"]
        else:  # pago
            total_visible = abs(ev["monto"])

        movimientos.append({
            "entrega_id": ev["id"] if ev["tipo"] == "entrega" else None,
            "fecha": ev["fecha"],
            "descripcion": ev["descripcion"],
            "total": total_visible,
            "saldo_posterior": saldo,
        })

    return jsonify({"ok": True, "movimientos": movimientos})


# ---------------------------
# DESCARGA PDF DE ENTREGA
# ---------------------------

@app.route("/entregas/<int:entrega_id>/pdf", methods=["GET"])
def descargar_pdf_entrega(entrega_id):
    conn = get_conn()
    cur = conn.cursor()

    # Cabecera
    cur.execute("""
        SELECT
            id,
            fecha,
            tipo_cliente,
            cliente_nombre,
            cantidad_total,
            total
        FROM entregas
        WHERE id = ?
    """, (entrega_id,))
    entrega = cur.fetchone()
    if not entrega:
        conn.close()
        return "Entrega no encontrada", 404

    # Ítems
    cur.execute("""
        SELECT
            nombre_pieza,
            cantidad,
            precio_unitario,
            total
        FROM entrega_items
        WHERE entrega_id = ?
        ORDER BY id
    """, (entrega_id,))
    raw_items = cur.fetchall()
    conn.close()

    items = []
    for it in raw_items:
        items.append({
            "pieza": it["nombre_pieza"],
            "cantidad": it["cantidad"],
            "precio": it["precio_unitario"],
            "total": it["total"],
        })

    # Limpiar nombre del cliente
    raw_cliente = entrega["cliente_nombre"] or ""
    cliente_limpio = re.sub(r'^\s*\d+\s*[-–.)_]*\s*', '', raw_cliente).strip()
    cliente_limpio = re.sub(r'^\.+', '', cliente_limpio).strip()
    cliente_limpio = re.sub(r'^[^\wÁÉÍÓÚáéíóúñÑ]+', '', cliente_limpio).strip()

    # Carpeta de salida
    output_dir = Path("pdfs")
    output_file = output_dir / f"entrega_{entrega_id}.pdf"

    pdf_bytes, pdf_path_str = build_entrega_pdf(
        cliente=cliente_limpio,
        fecha_iso=entrega["fecha"],
        items=items,
        out_path=output_file
    )

    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"ENTREGA_{cliente_limpio}.pdf"
    )


# ---------------------------
# API PAGOS: detalle + edición simple
# ---------------------------

@app.route("/api/pagos/<int:pago_id>", methods=["GET", "PUT"])
def api_pago_detalle_o_editar(pago_id):
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if request.method == "GET":
        cur.execute("SELECT * FROM pagos WHERE id = ?", (pago_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"ok": False, "error": "Pago no encontrado"}), 404
        return jsonify({"ok": True, "pago": dict(row)})

    # PUT: editar pago simple
    data = request.get_json(force=True) or {}

    fecha = (data.get("fecha") or "").strip() or datetime.now().strftime("%Y-%m-%d")
    descripcion = (data.get("descripcion") or "").strip()
    nombre_particular = (data.get("nombre_particular") or "").strip() or None
    categoria_precio = (data.get("categoria_precio") or "").strip() or "normal"

    revendedor_id = data.get("revendedor_id")
    if revendedor_id in ("", None):
        revendedor_id_int = None
        tipo_cliente = "particular"
    else:
        try:
            revendedor_id_int = int(revendedor_id)
        except Exception:
            conn.close()
            return jsonify({"ok": False, "error": "revendedor_id inválido"}), 200
        tipo_cliente = "revendedor"
        nombre_particular = None  # si es revendedor, no guardamos nombre particular

    try:
        monto = float(data.get("monto") or 0)
        division = int(data.get("division") or 1)
    except Exception:
        conn.close()
        return jsonify({"ok": False, "error": "Monto o división inválidos"}), 200

    if division <= 0:
        division = 1

    if monto <= 0:
        conn.close()
        return jsonify({"ok": False, "error": "El monto debe ser mayor a 0"}), 200

    mes_clave = fecha[:7] if len(fecha) >= 7 else datetime.now().strftime("%Y-%m")

    # Recalcular costos y ganancias
    costo = monto / division
    ganancia = monto - costo
    ganancia_individual = ganancia / 2.0

    cur.execute(
        """
        UPDATE pagos
        SET fecha = ?,
            tipo_cliente = ?,
            revendedor_id = ?,
            nombre_particular = ?,
            descripcion = ?,
            categoria_precio = ?,
            monto = ?,
            division = ?,
            costo = ?,
            ganancia = ?,
            ganancia_individual = ?,
            mes_clave = ?
        WHERE id = ?
        """,
        (
            fecha,
            tipo_cliente,
            revendedor_id_int,
            nombre_particular,
            descripcion,
            categoria_precio,
            monto,
            division,
            costo,
            ganancia,
            ganancia_individual,
            mes_clave,
            pago_id,
        ),
    )

    conn.commit()
    filas = cur.rowcount
    conn.close()

    if filas == 0:
        return jsonify({"ok": False, "error": "Pago no encontrado"}), 200

    return jsonify({"ok": True})


@app.route("/api/entregas/borrar", methods=["POST"])
def api_borrar_entrega():
    """
    Borra una entrega:
      - Restaura el stock de los productos involucrados
      - Elimina los items de la entrega
      - Elimina la cabecera de la entrega
    """
    try:
        data = request.get_json(force=True) or {}
        eid = data.get("id")

        if eid is None:
            return jsonify({"ok": False, "error": "Falta ID de entrega."}), 200

        try:
            eid_int = int(eid)
        except Exception:
            return jsonify({"ok": False, "error": "ID de entrega inválido."}), 200

        conn = get_conn()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 1) Traer items de la entrega
        cur.execute("""
            SELECT producto_id, cantidad
            FROM entrega_items
            WHERE entrega_id = ?
        """, (eid_int,))
        items = cur.fetchall()

        # 2) Restaurar stock
        for it in items:
            prod_id = it["producto_id"]
            cant = it["cantidad"] or 0

            if prod_id is None:
                continue

            cur.execute("SELECT stock FROM productos WHERE id = ?", (prod_id,))
            fila = cur.fetchone()
            if not fila:
                continue

            stock_actual = fila["stock"] or 0
            nuevo_stock = stock_actual + cant

            cur.execute("""
                UPDATE productos
                SET stock = ?, updated_at = datetime('now')
                WHERE id = ?
            """, (nuevo_stock, prod_id))

        # 3) Borrar items de la entrega
        cur.execute("DELETE FROM entrega_items WHERE entrega_id = ?", (eid_int,))

        # 4) Borrar cabecera de la entrega
        cur.execute("DELETE FROM entregas WHERE id = ?", (eid_int,))
        borradas = cur.rowcount

        conn.commit()
        conn.close()

        if borradas == 0:
            return jsonify({"ok": False, "error": "No se encontró entrega con ese ID."}), 200

        return jsonify({"ok": True, "borradas": borradas}), 200

    except Exception as e:
        return jsonify({"ok": False, "error": f"Error SQLite: {e}"}), 200


# ---------------------------
# API BORRAR GASTOS
# ---------------------------

@app.route("/api/gastos/borrar", methods=["POST"])
def api_borrar_gasto():
    try:
        data = request.get_json(force=True) or {}
        gid = data.get("id")

        if gid is None:
            return jsonify({"ok": False, "error": "Falta ID de gasto."}), 200

        try:
            gid_int = int(gid)
        except Exception:
            return jsonify({"ok": False, "error": "ID de gasto inválido."}), 200

        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM gastos WHERE id = ?", (gid_int,))
        conn.commit()
        borrados = cur.rowcount
        conn.close()

        if borrados == 0:
            return jsonify({"ok": False, "error": "No se encontró gasto con ese ID."}), 200

        return jsonify({"ok": True, "borrados": borrados}), 200

    except Exception as e:
        return jsonify({"ok": False, "error": f"Error SQLite: {e}"}), 200


# ---------------------------
# API BORRAR PAGOS
# ---------------------------

@app.route("/api/pagos/borrar", methods=["POST"])
def api_borrar_pagos():
    try:
        data = request.get_json(force=True) or {}
        ids = data.get("ids") or []

        if not isinstance(ids, list):
            return jsonify({"ok": False, "error": "Formato de IDs inválido (no es lista)."}), 200

        ids = [int(x) for x in ids if str(x).isdigit()]

        if not ids:
            return jsonify({"ok": False, "error": "Lista de IDs vacía después de filtrar."}), 200

        placeholders = ",".join("?" for _ in ids)

        conn = get_conn()
        cur = conn.cursor()
        cur.execute(f"DELETE FROM pagos WHERE id IN ({placeholders})", ids)
        conn.commit()
        borrados = cur.rowcount
        conn.close()

        if borrados == 0:
            return jsonify({
                "ok": False,
                "error": "No se encontró ningún pago con esos IDs."
            }), 200

        return jsonify({"ok": True, "borrados": borrados}), 200

    except Exception as e:
        return jsonify({"ok": False, "error": f"Error SQLite: {e}"}), 200


# ---------------------------
# MAIN
# ---------------------------

if __name__ == "__main__":
    print(f"Usando base de datos: {DB_PATH.resolve()}")
    app.run(host="0.0.0.0", port=5001, debug=False)
