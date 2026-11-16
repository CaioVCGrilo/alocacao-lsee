"""Motor de cálculo de scores, status, pendências, fila de prioridade e simulação."""

from __future__ import annotations

import math
from collections import Counter
from datetime import date, datetime
from typing import Any, Dict, List, Optional


def _to_float(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _to_int(val: Any) -> Optional[int]:
    if val is None or val == "":
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def _lookup_step(value: Optional[float], thresholds: List[tuple[float, float]]) -> Optional[float]:
    """Replica o comportamento de LOOKUP em faixas crescentes do Excel."""
    if value is None or not thresholds:
        return None
    matched = None
    for min_val, score in thresholds:
        if value >= min_val:
            matched = score
        else:
            break
    return matched


def compute_storage_score(
    arm_eq: float,
    arm_ref: float,
    escala_log: float,
    score_maximo: float,
) -> float:
    """Calcula a nota de armazenamento (Cenário B) em escala logarítmica até o teto."""
    if arm_eq <= 0 or arm_ref <= 0 or escala_log <= 0:
        return 0.0
    return min(
        score_maximo,
        score_maximo * math.log1p(arm_eq / escala_log) / math.log1p(arm_ref / escala_log),
    )


def build_arm_examples(rules: Dict[str, Any], arm_ref: float) -> List[Dict[str, Any]]:
    """Retorna exemplos práticos da escala de armazenamento no Cenário B."""
    peso_hd = rules["armaz_peso_hd"]
    escala = rules["armaz_escala_log"]
    score_max = rules["score_maximo"]
    samples = [
        ("Só HD 500 GB", 0.0, 500.0),
        ("Só HD 1 TB", 0.0, 1000.0),
        ("SSD 120 GB + HD 1 TB", 120.0, 1000.0),
        ("SSD 240 GB + HD 1 TB", 240.0, 1000.0),
        ("SSD 480 GB + HD 1 TB", 480.0, 1000.0),
        ("SSD 1 TB (sem HD)", 1000.0, 0.0),
        ("SSD 1 TB + HD 1 TB", 1000.0, 1000.0),
    ]
    out = []
    for label, ssd_gb, hd_gb in samples:
        arm_eq = max(0.0, ssd_gb + peso_hd * hd_gb)
        sc = compute_storage_score(arm_eq, arm_ref, escala, score_max)
        out.append({"label": label, "armaz_eq": round(arm_eq, 1), "score": round(sc, 2)})
    return out


def build_score_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    """Resumo não sensível dos critérios de pontuação para explicação simplificada."""
    r = state["rules"]
    dash = state["dashboard"]
    return {
        "pesos": {
            "cpu": r["peso_cpu"],
            "ram": r["peso_ram"],
            "armazenamento": r["peso_armazenamento"],
            "gpu": r["peso_gpu"],
        },
        "armaz_peso_hd": r["armaz_peso_hd"],
        "armaz_escala_log": r["armaz_escala_log"],
        "score_maximo": r["score_maximo"],
        "arm_ref": dash["arm_ref"],
        "arm_ref_pcs": dash["arm_ref_pcs"],
        "arm_examples": build_arm_examples(r, dash["arm_ref"]),
    }


def load_rules(conn: Any) -> Dict[str, Any]:
    """Carrega parâmetros, faixas de score e regras de categoria do banco."""
    params_rows = conn.execute("SELECT `key`, label, `value`, category FROM system_params").fetchall()
    params = {r["key"]: r["value"] for r in params_rows}
    params_meta = [dict(r) for r in params_rows]

    thresholds: Dict[str, List[tuple[float, float]]] = {"ram": [], "hd": [], "ssd": []}
    thresh_rows = conn.execute(
        "SELECT id, metric, min_value, score FROM score_thresholds ORDER BY metric, min_value"
    ).fetchall()
    for r in thresh_rows:
        thresholds.setdefault(r["metric"], []).append((float(r["min_value"]), float(r["score"])))

    cat_rows = conn.execute(
        "SELECT name, priority_group, participa_upgrade, precisa_pc FROM categories"
    ).fetchall()
    categories = {r["name"]: dict(r) for r in cat_rows}

    def _num_param(key: str, default: float) -> float:
        val = _to_float(params.get(key))
        return val if val is not None else default

    armaz_peso_hd = _to_float(params.get("armaz_peso_hd"))
    armaz_escala_log = _to_float(params.get("armaz_escala_log"))

    return {
        "params": params,
        "params_meta": params_meta,
        "thresholds": thresholds,
        "thresholds_rows": [dict(r) for r in thresh_rows],
        "categories": categories,
        "peso_cpu": max(0.0, _num_param("peso_cpu", 0.45)),
        "peso_ram": max(0.0, _num_param("peso_ram", 0.30)),
        "peso_armazenamento": max(0.0, _num_param("peso_armazenamento", 0.225)),
        "peso_gpu": max(0.0, _num_param("peso_gpu", 0.025)),
        "armaz_peso_hd": armaz_peso_hd if armaz_peso_hd is not None else 0.25,
        "armaz_escala_log": armaz_escala_log if armaz_escala_log and armaz_escala_log > 0 else 60.0,
        "score_maximo": _num_param("score_maximo", 10.0) or 10.0,
        "diff_min_upgrade": max(0.0, _num_param("diferenca_minima_upgrade", 3.0)),
        "public_anonymize_names": params.get("public_anonymize_names", "Não") == "Sim",
    }


def _build_allocation_index(conn: Any) -> Dict[str, Any]:
    rows = conn.execute(
        """
        SELECT id, id_alocacao, pessoa_nome, id_pessoa, pc_id_pc,
               data_inicio, data_fim, tipo, motivo_inicio, motivo_fim, observacoes, origem
        FROM allocations
        ORDER BY id
        """
    ).fetchall()
    all_alocs = []
    active_by_pc: Dict[str, List[Dict[str, Any]]] = {}
    active_by_person_id: Dict[str, List[Dict[str, Any]]] = {}
    active_by_person_name: Dict[str, List[Dict[str, Any]]] = {}

    for r in rows:
        item = dict(r)
        ativa = not (item["data_fim"] or "").strip()
        item["ativa"] = ativa
        all_alocs.append(item)
        if ativa:
            pc_code = (item["pc_id_pc"] or "").strip()
            if pc_code:
                active_by_pc.setdefault(pc_code, []).append(item)
            pid = (item["id_pessoa"] or "").strip()
            pnome = (item["pessoa_nome"] or "").strip()
            if pid:
                active_by_person_id.setdefault(pid, []).append(item)
            if pnome:
                active_by_person_name.setdefault(pnome, []).append(item)

    return {
        "all": all_alocs,
        "active_by_pc": active_by_pc,
        "active_by_person_id": active_by_person_id,
        "active_by_person_name": active_by_person_name,
    }


def compute_state(conn: Any) -> Dict[str, Any]:
    """Calcula o estado completo e consistente do sistema a partir do banco."""
    rules = load_rules(conn)
    aloc_idx = _build_allocation_index(conn)

    proc_rows = conn.execute(
        "SELECT id, name, threads, multi, single, indice_cpu, fonte, data_referencia, observacoes FROM processors ORDER BY name"
    ).fetchall()
    processors = [dict(r) for r in proc_rows]
    proc_by_name = {p["name"]: p for p in processors}

    pc_rows = conn.execute(
        """
        SELECT id, id_pc, patrimonio, processor_name, ram_gb, ssd_gb, hd_gb,
               possui_gpu, tipo_de_uso, status_manual, localizacao, projeto, observacoes,
               score_cpu_legado, score_armaz_legado
        FROM computers
        ORDER BY id
        """
    ).fetchall()
    raw_pcs = [dict(r) for r in pc_rows]
    id_pc_counts = Counter((r["id_pc"] or "").strip() for r in raw_pcs if (r["id_pc"] or "").strip())

    # Primeira passagem: status preliminar e referências dinâmicas (CPU e Armazenamento)
    prelim = []
    cpu_ref_candidates: List[float] = []
    arm_ref_candidates: List[tuple[float, str]] = []
    peso_hd = rules["armaz_peso_hd"]
    for pc in raw_pcs:
        id_pc = (pc["id_pc"] or "").strip()
        proc = proc_by_name.get((pc["processor_name"] or "").strip())
        indice_cpu = float(proc["indice_cpu"]) if proc else None
        tipo = (pc["tipo_de_uso"] or "").strip()
        status_manual = (pc["status_manual"] or "").strip()
        ssd_gb = _to_float(pc["ssd_gb"])
        hd_gb = _to_float(pc["hd_gb"])

        if not id_pc:
            status = ""
        elif status_manual:
            status = status_manual
        elif tipo == "Individual":
            status = "Em uso" if id_pc in aloc_idx["active_by_pc"] else "Livre"
        elif tipo in ("Compartilhado IC", "Especial", "Sala de Reunião"):
            status = "Em uso"
        else:
            status = ""

        if tipo == "Individual" and status != "Despatrimoniado" and indice_cpu is not None:
            cpu_ref_candidates.append(indice_cpu)

        arm_eq: Optional[float] = None
        if ssd_gb is not None or hd_gb is not None:
            arm_eq = max(0.0, (ssd_gb or 0.0) + peso_hd * (hd_gb or 0.0))
            if tipo == "Individual" and status != "Despatrimoniado" and arm_eq > 0:
                arm_ref_candidates.append((arm_eq, id_pc))

        pc["id_pc_clean"] = id_pc
        pc["indice_cpu"] = indice_cpu
        pc["status"] = status
        pc["arm_eq"] = arm_eq
        prelim.append(pc)

    cpu_ref = max(cpu_ref_candidates) if cpu_ref_candidates else 0.0
    arm_ref = max((val for val, _ in arm_ref_candidates), default=0.0)
    arm_ref_pcs = sorted(pid for val, pid in arm_ref_candidates if val == arm_ref and pid) if arm_ref > 0 else []

    # Segunda passagem: scores, usuário atual e pendências
    computers: List[Dict[str, Any]] = []
    computers_by_id: Dict[str, Dict[str, Any]] = {}
    for pc in prelim:
        id_pc = pc["id_pc_clean"]
        indice_cpu = pc["indice_cpu"]
        ram_gb = _to_float(pc["ram_gb"])
        ssd_gb = _to_float(pc["ssd_gb"])
        hd_gb = _to_float(pc["hd_gb"])
        gpu_flag = (pc["possui_gpu"] or "").strip()
        tipo = (pc["tipo_de_uso"] or "").strip()
        status = pc["status"]
        arm_eq = pc["arm_eq"]

        score_cpu = min(10.0, indice_cpu / cpu_ref * 10.0) if (indice_cpu is not None and cpu_ref > 0) else None
        score_ram = _lookup_step(ram_gb, rules["thresholds"]["ram"])

        if not id_pc:
            score_arm: Optional[float] = None
        elif ssd_gb is None and hd_gb is None:
            score_arm = _to_float(pc["score_armaz_legado"])
        elif arm_eq is not None:
            score_arm = compute_storage_score(
                arm_eq,
                arm_ref,
                rules["armaz_escala_log"],
                rules["score_maximo"],
            )
        else:
            score_arm = None

        if gpu_flag == "Sim":
            score_gpu: Optional[float] = 10.0
        elif gpu_flag == "Não":
            score_gpu = 0.0
        else:
            score_gpu = None

        if None not in (score_cpu, score_ram, score_arm, score_gpu):
            score_total: Optional[float] = (
                score_cpu * rules["peso_cpu"] * 10.0
                + score_ram * rules["peso_ram"] * 10.0
                + score_arm * rules["peso_armazenamento"] * 10.0
                + score_gpu * rules["peso_gpu"] * 10.0
            )
        else:
            score_total = None

        active_list = aloc_idx["active_by_pc"].get(id_pc, []) if id_pc else []
        usuario_atual = active_list[0]["pessoa_nome"] if active_list else ""
        usuario_atual_id = active_list[0]["id_pessoa"] if active_list else ""

        pendencias = []
        if not id_pc:
            pendencias.append("ID_PC")
        if not (pc["processor_name"] or "").strip():
            pendencias.append("Processador")
        if ram_gb is None:
            pendencias.append("RAM")
        if ssd_gb is None:
            pendencias.append("SSD")
        if hd_gb is None:
            pendencias.append("HD")
        if not gpu_flag:
            pendencias.append("GPU")
        if not tipo:
            pendencias.append("Tipo de uso")
        if (pc["processor_name"] or "").strip() and indice_cpu is None:
            pendencias.append("Benchmark CPU")
        if id_pc and id_pc_counts[id_pc] > 1:
            pendencias.append("ID duplicado")
        if status == "Livre" and usuario_atual:
            pendencias.append("Livre com alocação ativa")
        if tipo == "Individual" and status == "Em uso" and not usuario_atual:
            pendencias.append("Em uso sem alocação")

        item = {
            "id": pc["id"],
            "id_pc": id_pc,
            "patrimonio": pc["patrimonio"] or "",
            "processador": pc["processor_name"] or "",
            "ram_gb": ram_gb,
            "ssd_gb": ssd_gb,
            "hd_gb": hd_gb,
            "possui_gpu": gpu_flag,
            "tipo_de_uso": tipo,
            "status_manual": pc["status_manual"] or "",
            "status": status,
            "localizacao": pc["localizacao"] or "",
            "projeto": pc["projeto"] or "",
            "observacoes": pc["observacoes"] or "",
            "indice_cpu": indice_cpu,
            "score_cpu": round(score_cpu, 4) if score_cpu is not None else None,
            "score_ram": score_ram,
            "score_armazenamento": round(score_arm, 4) if score_arm is not None else None,
            "armaz_eq": round(arm_eq, 1) if arm_eq is not None else None,
            "score_gpu": score_gpu,
            "score_total": score_total,
            "score_total_fmt": round(score_total, 2) if score_total is not None else None,
            "usuario_atual": usuario_atual,
            "usuario_atual_id": usuario_atual_id,
            "validacao_dados": ("OK" if not pendencias else "INCOMPLETO") if id_pc else "",
            "pendencia": "; ".join(pendencias),
            "score_cpu_legado": _to_float(pc["score_cpu_legado"]),
            "score_armaz_legado": _to_float(pc["score_armaz_legado"]),
        }
        computers.append(item)
        if id_pc and id_pc not in computers_by_id:
            computers_by_id[id_pc] = item

    # Pessoas e fila de prioridade
    people_rows = conn.execute(
        """
        SELECT id, id_pessoa, nome, categoria, ano_entrada, semestre_entrada,
               situacao, data_saida, observacoes, data_ultimo_upgrade
        FROM people
        ORDER BY id
        """
    ).fetchall()

    people_prelim = []
    valid_keys: List[int] = []
    for r in people_rows:
        p = dict(r)
        nome = (p["nome"] or "").strip()
        pid = (p["id_pessoa"] or "").strip()
        cat_info = rules["categories"].get(p["categoria"], {})
        participa_upgrade = (
            "Sim" if p["situacao"] == "Ativo" and cat_info.get("participa_upgrade") == 1 else "Não"
        )
        person_alocs = aloc_idx["active_by_person_id"].get(pid) or aloc_idx["active_by_person_name"].get(nome) or []
        qtd_pcs = len(person_alocs)
        precisa_pc = "Sim" if participa_upgrade == "Sim" and qtd_pcs == 0 else "Não"

        pc_principal = ""
        for al in person_alocs:
            if al["tipo"] == "Principal":
                pc_principal = al["pc_id_pc"]
                break

        pc_obj = computers_by_id.get(pc_principal)
        score_pc_principal = pc_obj["score_total"] if pc_obj else None

        ano = _to_int(p["ano_entrada"])
        sem = _to_int(p["semestre_entrada"])
        if participa_upgrade == "Sim" and ano is not None and sem is not None:
            grp = cat_info.get("priority_group")
            prefix = (int(grp) * 100000) if (grp is not None and int(grp) >= 1) else 900000
            chave_prioridade: Optional[int] = prefix + ano * 10 + sem
            valid_keys.append(chave_prioridade)
        else:
            chave_prioridade = None

        excepcionais = sum(1 for al in person_alocs if al["tipo"] == "Excepcional")
        p_pend = []
        if not pid:
            p_pend.append("ID_Pessoa")
        if not nome:
            p_pend.append("Nome")
        if not p["categoria"]:
            p_pend.append("Categoria")
        if not p["situacao"]:
            p_pend.append("Situação")
        if participa_upgrade == "Sim" and ano is None:
            p_pend.append("Ano de entrada")
        if participa_upgrade == "Sim" and sem is None:
            p_pend.append("Semestre de entrada")
        if qtd_pcs > 1 and excepcionais < (qtd_pcs - 1):
            p_pend.append("Mais de um PC sem Excepcional")

        p.update(
            {
                "participa_upgrade": participa_upgrade,
                "precisa_pc": precisa_pc,
                "quantidade_pcs": qtd_pcs,
                "pc_principal": pc_principal,
                "score_pc_principal": score_pc_principal,
                "score_pc_principal_fmt": round(score_pc_principal, 2) if score_pc_principal is not None else None,
                "chave_prioridade": chave_prioridade,
                "validacao_dados": "OK" if not p_pend else "INCOMPLETO",
                "pendencia": "; ".join(p_pend),
            }
        )
        people_prelim.append(p)

    key_counts = Counter(valid_keys)
    people: List[Dict[str, Any]] = []
    for p in people_prelim:
        cp = p["chave_prioridade"]
        if cp is not None:
            ordem = sum(1 for k in valid_keys if k < cp) + 1
            empate = "SIM" if key_counts[cp] > 1 else ""
        else:
            ordem = None
            empate = ""
        p["ordem_prioridade"] = ordem
        p["empate_prioridade"] = empate
        people.append(p)

    # Enriquecer alocações com validação
    pc_active_counts = {pc_code: len(lst) for pc_code, lst in aloc_idx["active_by_pc"].items()}
    person_by_name = {p["nome"]: p for p in people}
    allocations: List[Dict[str, Any]] = []
    for al in aloc_idx["all"]:
        pend_al = []
        if not al["pessoa_nome"]:
            pend_al.append("Pessoa")
        elif al["pessoa_nome"] not in person_by_name and not al["id_pessoa"]:
            pend_al.append("Pessoa não encontrada")
        if not al["pc_id_pc"]:
            pend_al.append("PC")
        if not al["tipo"]:
            pend_al.append("Tipo")
        if not al["motivo_inicio"]:
            pend_al.append("Motivo início")
        if not al["origem"]:
            pend_al.append("Origem")
        if al["origem"] != "Migração inicial" and not al["data_inicio"]:
            pend_al.append("Data início")
        if al["data_fim"] and not al["motivo_fim"]:
            pend_al.append("Motivo fim")
        if al["ativa"] and al["pc_id_pc"] and pc_active_counts.get(al["pc_id_pc"], 0) > 1:
            pend_al.append("PC em múltiplas alocações ativas")

        al["validacao_dados"] = "OK" if not pend_al else "INCOMPLETO"
        al["pendencia"] = "; ".join(pend_al)
        allocations.append(al)

    # Fila ordenada
    fila = sorted(
        [p for p in people if p["chave_prioridade"] is not None],
        key=lambda x: (x["chave_prioridade"], x["id"]),
    )

    # Métricas do Dashboard
    pcs_indiv = [c for c in computers if c["tipo_de_uso"] == "Individual"]
    pcs_livres = [c for c in pcs_indiv if c["status"] == "Livre"]
    pcs_livres_sorted = sorted(
        pcs_livres,
        key=lambda c: (c["score_total"] is not None, c["score_total"] or -1.0),
        reverse=True,
    )

    top_livres = []
    for rank, pc in enumerate(pcs_livres_sorted[:5], start=1):
        sc = pc["score_total"]
        candidatos = 0
        if sc is not None:
            candidatos = sum(
                1
                for p in people
                if p["participa_upgrade"] == "Sim"
                and p["pc_principal"]
                and p["score_pc_principal"] is not None
                and p["score_pc_principal"] < (sc - rules["diff_min_upgrade"])
            )
        top_livres.append(
            {
                "rank": rank,
                "id_pc": pc["id_pc"],
                "processador": pc["processador"],
                "ram_gb": pc["ram_gb"],
                "ssd_gb": pc["ssd_gb"],
                "hd_gb": pc["hd_gb"],
                "possui_gpu": pc["possui_gpu"],
                "localizacao": pc["localizacao"],
                "score_total": pc["score_total"],
                "score_total_fmt": pc["score_total_fmt"],
                "candidatos": candidatos,
            }
        )

    pessoas_sem_pc = [p for p in fila if p["precisa_pc"] == "Sim"]

    dashboard = {
        "pcs_individuais": len(pcs_indiv),
        "em_uso": sum(1 for c in pcs_indiv if c["status"] == "Em uso"),
        "livres": len(pcs_livres),
        "manutencao": sum(1 for c in pcs_indiv if c["status"] == "Manutenção"),
        "pessoas_sem_pc": sum(1 for p in people if p["precisa_pc"] == "Sim"),
        "pcs_incompletos": sum(1 for c in computers if c["validacao_dados"] == "INCOMPLETO"),
        "cpu_ref": cpu_ref,
        "arm_ref": round(arm_ref, 1),
        "arm_ref_pcs": arm_ref_pcs,
        "top_pcs_livres": top_livres,
        "pessoas_sem_pc_lista": pessoas_sem_pc,
        "fila_prioridade": fila,
    }

    return {
        "rules": rules,
        "processors": processors,
        "computers": computers,
        "computers_by_id": computers_by_id,
        "people": people,
        "allocations": allocations,
        "fila": fila,
        "dashboard": dashboard,
    }


def _months_since(date_str: str) -> str:
    raw = (date_str or "").strip()
    if not raw:
        return "Sem histórico"
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            dt = datetime.strptime(raw, fmt).date()
            today = date.today()
            months = (today.year - dt.year) * 12 + (today.month - dt.month)
            return f"{months} meses"
        except ValueError:
            continue
    return raw


def simulate_upgrade(state: Dict[str, Any], pc_id: str) -> Dict[str, Any]:
    """Simula a fila de elegibilidade para um PC ofertado."""
    pc = state["computers_by_id"].get((pc_id or "").strip())
    if not pc:
        return {"pc": None, "rows": []}

    score_oferecido = pc["score_total"]
    diff_min = state["rules"]["diff_min_upgrade"]

    # Mesmo ordenamento da aba Simulador_Upgrade: participantes em ordem de fila, depois demais
    ordered_people = list(state["fila"]) + [
        p for p in state["people"] if p["chave_prioridade"] is None
    ]

    sim_rows = []
    for p in ordered_people:
        score_atual = p["score_pc_principal"]
        ganho = (
            (score_oferecido - score_atual)
            if (score_oferecido is not None and score_atual is not None)
            else None
        )

        if p["participa_upgrade"] != "Sim":
            resultado = "NÃO PARTICIPA"
        elif p["ordem_prioridade"] is None:
            resultado = "PRIORIDADE INCOMPLETA"
        elif not p["pc_principal"]:
            resultado = "SEM PC - APÓS UPGRADES"
        elif ganho is not None and ganho > diff_min:
            resultado = "ELEGÍVEL"
        else:
            resultado = "NÃO ELEGÍVEL"

        sim_rows.append(
            {
                "id_pessoa": p["id_pessoa"],
                "ordem": p["ordem_prioridade"],
                "pessoa": p["nome"],
                "categoria": p["categoria"],
                "entrada": (
                    f"{p['ano_entrada']}/{p['semestre_entrada']}"
                    if p["ano_entrada"] and p["semestre_entrada"]
                    else ""
                ),
                "situacao": p["situacao"],
                "pc_principal": p["pc_principal"],
                "score_atual": score_atual,
                "score_atual_fmt": round(score_atual, 2) if score_atual is not None else None,
                "score_oferecido": score_oferecido,
                "score_oferecido_fmt": round(score_oferecido, 2) if score_oferecido is not None else None,
                "ganho": ganho,
                "ganho_fmt": round(ganho, 2) if ganho is not None else None,
                "resultado": resultado,
                "ultimo_upgrade": _months_since(p["data_ultimo_upgrade"]),
            }
        )

    return {
        "pc": {
            "id_pc": pc["id_pc"],
            "processador": pc["processador"],
            "ram_gb": pc["ram_gb"],
            "ssd_gb": pc["ssd_gb"],
            "hd_gb": pc["hd_gb"],
            "possui_gpu": pc["possui_gpu"],
            "status": pc["status"],
            "tipo_de_uso": pc["tipo_de_uso"],
            "score_total": score_oferecido,
            "score_total_fmt": pc["score_total_fmt"],
        },
        "diff_min_upgrade": diff_min,
        "rows": sim_rows,
    }
