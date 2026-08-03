"""Generate synthetic SAP-Note-style PDFs for demoing the POC.

Real SAP Notes are SAP-proprietary content, so the repo ships none. This
script fabricates eight plausible notes (correct layout: header line,
Symptom / Environment / Cause / Resolution / Keywords sections, component
in header data) purely so the pipeline has something to chew on. The note
numbers and technical content are invented.

Usage:
    python -m scripts.make_sample_notes            # writes to ./data/notes
    python -m scripts.make_sample_notes --out DIR
"""

from __future__ import annotations

import argparse
from pathlib import Path

from fpdf import FPDF

NOTES = [
    {
        "id": "2458901",
        "title": "Indexserver crashes with OOM during delta merge",
        "component": "HAN-DB",
        "sections": {
            "Symptom": (
                "The SAP HANA indexserver terminates unexpectedly during a delta merge "
                "operation. The crash dump shows an out-of-memory (OOM) situation with "
                "composite limit violation. Trace files contain the error "
                "'Memory allocation failed; failed to allocate 2097152 bytes' and the alert "
                "trace shows event 'MergedogMonitor: delta merge failed'. End users experience "
                "dropped connections and transactions are rolled back."
            ),
            "Environment": "SAP HANA Platform Edition 2.0 SPS05 to SPS07. Scale-up and scale-out systems.",
            "Cause": (
                "The delta storage of one or more column store tables has grown very large "
                "because auto merge was disabled or the mergedog parameters were tuned too "
                "conservatively. During the merge, both the delta and the new main storage "
                "must be held in memory simultaneously, which exceeds the global allocation "
                "limit. In several reported cases a single partition exceeded 500 GB of delta."
            ),
            "Resolution": (
                "1. Check the delta size of the largest tables with the SQL statement in the "
                "attached file M_CS_TABLES_delta.sql and identify tables with delta storage "
                "above 10% of main storage.\n"
                "2. Verify that automerge is active: SELECT * FROM M_INIFILE_CONTENTS WHERE "
                "KEY = 'active' AND SECTION = 'mergedog'. The value must be 'yes'.\n"
                "3. For very large tables, perform a manual merge in a maintenance window: "
                "MERGE DELTA OF \"<schema>\".\"<table>\".\n"
                "4. Consider partitioning tables whose partitions exceed 100 GB so merges "
                "operate on smaller units.\n"
                "5. If the global_allocation_limit is set below the recommended value, adjust "
                "it according to the sizing report. As a stopgap, token 'critical merge' can "
                "be given more headroom by lowering statement_memory_limit for heavy queries."
            ),
            "Keywords": "indexserver crash, OOM, delta merge, mergedog, composite limit, column store",
        },
    },
    {
        "id": "2731402",
        "title": "CALL_FUNCTION_SEND_ERROR during RFC after kernel upgrade",
        "component": "BC-MID-RFC",
        "sections": {
            "Symptom": (
                "After a kernel patch or upgrade, RFC calls between systems fail sporadically "
                "with the short dump CALL_FUNCTION_SEND_ERROR in transaction ST22. The dump "
                "text shows 'connection closed (no data)'. The dev_rfc trace contains the "
                "message 'NiIRead: SiRecv failed for hdl'. Affected interfaces include ALE/IDoc "
                "distribution and BW extraction."
            ),
            "Environment": "SAP NetWeaver Application Server ABAP 7.50 and higher. Kernel 7.53, 7.54, 7.77, 7.85.",
            "Cause": (
                "The gateway keepalive handling changed with the newer kernel. If the parameter "
                "gw/keepalive is set to a value lower than the network idle timeout of an "
                "intermediate firewall, established RFC connections are dropped by the firewall "
                "while both endpoints still consider them open. The next send on the stale "
                "connection fails with CALL_FUNCTION_SEND_ERROR."
            ),
            "Resolution": (
                "1. Check the current values of gw/keepalive and gw/gw_disconnect in RZ11 on "
                "both systems.\n"
                "2. Set gw/keepalive to a value below the firewall idle timeout (recommended: "
                "300 seconds when the firewall drops idle sessions after 600 seconds).\n"
                "3. Restart the gateway via SMGW (Goto -> Expert Functions) or plan an instance "
                "restart.\n"
                "4. If dumps persist, activate the RFC trace (RZ11: rfc/trace = 1) and check "
                "dev_rfc for NiIRead errors to confirm the connection is closed by the network, "
                "not the partner system.\n"
                "5. Apply the latest kernel patch level of your release; the send-retry handling "
                "was improved."
            ),
            "Keywords": "CALL_FUNCTION_SEND_ERROR, RFC, gateway, keepalive, NiIRead, SiRecv, ST22",
        },
    },
    {
        "id": "2896554",
        "title": "Fiori launchpad tiles fail with 'App could not be opened' after upgrade",
        "component": "CA-FLP-ABA",
        "sections": {
            "Symptom": (
                "After upgrading the frontend server or applying a SAPUI5 patch, opening certain "
                "tiles in the SAP Fiori launchpad fails with the message 'App could not be "
                "opened because the SAP UI5 component of the application could not be loaded'. "
                "The browser console shows HTTP 404 for Component-preload.js under "
                "/sap/bc/ui5_ui5/."
            ),
            "Environment": "SAP Fiori front-end server 6.0/2020 onwards, embedded or hub deployment. SAPUI5 1.71 and higher.",
            "Cause": (
                "The UI5 application index is outdated after the upgrade. The launchpad resolves "
                "the component path from the index tables (/UI5/APPIDX), which still reference "
                "the pre-upgrade BSP application version. Cached launchpad content in the "
                "browser and on the server intensifies the issue."
            ),
            "Resolution": (
                "1. Run report /UI5/APP_INDEX_CALCULATE in transaction SA38 on the frontend "
                "server (full run, all repositories).\n"
                "2. Invalidate the launchpad caches: run /UI2/INVALIDATE_GLOBAL_CACHES and "
                "/UI2/CHIP_SYNCHRONIZE_CACHE.\n"
                "3. Clear the ICM server cache in SMICM (Goto -> HTTP Plug-In -> Server Cache "
                "-> Invalidate Globally).\n"
                "4. Ask users to clear the browser cache or perform a hard reload.\n"
                "5. If single apps remain broken, check SICF that the service for the BSP "
                "application is active."
            ),
            "Keywords": "Fiori launchpad, tile, App could not be opened, Component-preload, /UI5/APP_INDEX_CALCULATE",
        },
    },
    {
        "id": "1943765",
        "title": "Transport import hangs - RDDIMPDP not triggered",
        "component": "BC-CTS-TMS",
        "sections": {
            "Symptom": (
                "A transport request remains in status 'Import running' for hours in STMS. The "
                "import queue does not progress. Transaction SE01 shows the request stuck in "
                "phase 'DDIC import' or 'Move nametabs'. tp system log shows 'WARNING: "
                "background job RDDIMPDP could not be started or terminated abnormally'."
            ),
            "Environment": "All SAP NetWeaver ABAP releases using the Change and Transport System.",
            "Cause": (
                "The background job RDDIMPDP is not scheduled in client 000, or the background "
                "processing system has no free BTC work processes. RDDIMPDP is event-triggered "
                "(SAP_TRIGGER_RDDIMPDP) and performs the ABAP-side import steps; without it, tp "
                "waits indefinitely."
            ),
            "Resolution": (
                "1. Log on to client 000 as a user with batch authorization and run report "
                "RDDNEWPP in SE38. This schedules RDDIMPDP as an event-periodic job.\n"
                "2. Check in SM37 that RDDIMPDP exists with status 'Released' and event "
                "SAP_TRIGGER_RDDIMPDP.\n"
                "3. Verify free background work processes in SM50/SM66; increase "
                "rdisp/wp_no_btc if all BTC processes are permanently busy.\n"
                "4. Re-trigger the stuck import: in STMS select the request and choose Import "
                "again, or run 'tp import' from the command line.\n"
                "5. If the job aborts, check SM37 job log and ST22 for dumps in the DDIC "
                "activation step."
            ),
            "Keywords": "RDDIMPDP, RDDNEWPP, transport hangs, STMS, import queue, SAP_TRIGGER_RDDIMPDP",
        },
    },
    {
        "id": "2085934",
        "title": "Background job cancelled: ORA-01653 unable to extend table segment",
        "component": "BC-DB-ORA",
        "sections": {
            "Symptom": (
                "Long-running background jobs terminate with SQL error 1653. The job log shows "
                "'ORA-01653: unable to extend table SAPSR3.<table> by 8192 in tablespace "
                "PSAPSR3'. System log SM21 records database error 1653 for the same tablespace."
            ),
            "Environment": "SAP systems on Oracle Database 12c/19c with dictionary or locally managed tablespaces.",
            "Cause": (
                "The tablespace has no free extents left and autoextend is disabled for its "
                "data files, or the file system / ASM disk group underneath is full. Heavy "
                "inserts from interfaces or archiving backlogs are typical growth drivers."
            ),
            "Resolution": (
                "1. Check free space in DB02 (or DBACOCKPIT -> Space -> Tablespaces) for the "
                "affected tablespace.\n"
                "2. Add a new data file or enable autoextend: ALTER DATABASE DATAFILE '<file>' "
                "AUTOEXTEND ON NEXT 100M MAXSIZE 32767M; ensure the underlying volume has "
                "space.\n"
                "3. Restart the cancelled job after the extension.\n"
                "4. Mid-term: analyze the fastest-growing tables in DB02 history, set up data "
                "archiving for the top growers, and review interface logging levels.\n"
                "5. Set up an alert threshold (e.g. 90% tablespace usage) in DBACOCKPIT to "
                "catch the situation before jobs cancel."
            ),
            "Keywords": "ORA-01653, tablespace full, PSAPSR3, autoextend, DB02, background job cancelled",
        },
    },
    {
        "id": "3012877",
        "title": "SPAM/SAINT stops in phase IMPORT_PROPER with TP_STEP_FAILURE",
        "component": "BC-UPG-OCS",
        "sections": {
            "Symptom": (
                "During support package import, SPAM stops with error TP_STEP_FAILURE in phase "
                "IMPORT_PROPER. The import log shows return code 0008 and 'function module "
                "does not exist or EXCEPTION raised'. No further packages can be imported and "
                "the queue cannot be reset."
            ),
            "Environment": "All ABAP systems importing support packages with SPAM/SAINT.",
            "Cause": (
                "A step of the transport (typically the method execution step XPRA or after-"
                "import method) failed. Frequent root causes are: inactive DDIC objects from a "
                "previous import, missing SPAM update (SPAM version older than required), or a "
                "terminated RDDEXECL job due to lacking background resources."
            ),
            "Resolution": (
                "1. Read the exact failing step in the SPAM log (Goto -> Log -> Queue). "
                "Identify whether XPRA_EXECUTION or DDIC_ACTIVATION failed.\n"
                "2. Check ST22 for dumps of user DDIC and SM37 for cancelled RDDEXECL jobs at "
                "the failure time.\n"
                "3. Update SPAM/SAINT to the latest version first if the log demands it.\n"
                "4. For inactive DDIC objects, activate them in SE11 or run mass activation "
                "with report RADMASG0, then continue the queue in SPAM.\n"
                "5. Only reset the queue with the documented SPAM reset procedure; never "
                "delete queue entries at database level."
            ),
            "Keywords": "SPAM, SAINT, TP_STEP_FAILURE, IMPORT_PROPER, XPRA, RC 0008, RDDEXECL",
        },
    },
    {
        "id": "2649310",
        "title": "SM58 shows tRFC entries stuck in status SYSFAIL after IDoc dispatch",
        "component": "BC-MID-ALE",
        "sections": {
            "Symptom": (
                "Outbound IDocs remain in status 03 but are not received by the partner "
                "system. Transaction SM58 lists transactional RFC entries with status text "
                "SYSFAIL and error 'No service for system SAPXXX, client 100 in Integration "
                "Directory' or 'Password logon no longer possible - too many failed attempts'."
            ),
            "Environment": "ALE/IDoc scenarios between ABAP systems or via PI/PO. All NetWeaver releases.",
            "Cause": (
                "The RFC destination used by the tRFC/qRFC scheduler is misconfigured: the "
                "service user in the destination is locked or has an expired password, or the "
                "logical system mapping changed after a system copy. The tRFC entry stays in "
                "SYSFAIL and is retried by report RSARFCEX according to the destination's "
                "retry settings."
            ),
            "Resolution": (
                "1. In SM58, read the exact error text of the failing entry (double-click).\n"
                "2. Test the RFC destination in SM59 (Utilities -> Test -> Connection and "
                "Authorization). Unlock the service user / reset the password in SU01 on the "
                "target system if authentication fails.\n"
                "3. After fixing the destination, reprocess the LUWs: in SM58 choose Edit -> "
                "Execute LUW, or schedule report RSARFCEX for mass reprocessing.\n"
                "4. After system copies, check the logical system names in BD54 and the "
                "partner profiles in WE20 to ensure the mapping matches the new landscape.\n"
                "5. Monitor with SM58 date range and RSARFCRD to confirm the backlog drains."
            ),
            "Keywords": "SM58, tRFC, SYSFAIL, IDoc status 03, RSARFCEX, RFC destination, ALE",
        },
    },
    {
        "id": "2377120",
        "title": "Work processes in PRIV mode - extended memory exhausted",
        "component": "BC-CST-MM",
        "sections": {
            "Symptom": (
                "Dialog response times degrade sharply. SM50 shows several dialog work "
                "processes in mode PRIV that no longer serve other users. SM04 shows single "
                "sessions consuming gigabytes. Eventually users receive TSV_TNEW_PAGE_ALLOC_"
                "FAILED short dumps."
            ),
            "Environment": "SAP NetWeaver AS ABAP on any database. 64-bit kernels.",
            "Cause": (
                "A user context could not be rolled out because it exceeded em/blocksize and "
                "the configured extended memory (em/initial_size_MB) was exhausted, so the "
                "work process switched to PRIV mode and holds its memory privately. Common "
                "triggers are ALV reports over unrestricted date ranges and custom reports "
                "reading entire tables into internal tables."
            ),
            "Resolution": (
                "1. Identify the memory-heavy transactions in SM04 / ST02 (History -> Top 25).\n"
                "2. Educate users / fix custom code: add mandatory selection criteria, use "
                "package processing (SELECT ... PACKAGE SIZE), and free internal tables.\n"
                "3. Review memory parameters against the current sizing: em/initial_size_MB, "
                "abap/heap_area_dia, abap/heap_area_total. Increase extended memory only if "
                "the host has free RAM.\n"
                "4. As protection, set rdisp/max_priv_time (e.g. 600 seconds) so PRIV "
                "processes are reclaimed, and abap/heaplimit so oversized contexts trigger a "
                "restart of the work process after rollout.\n"
                "5. For recurring offenders, set up SM04 memory alerts in CCMS/Solution "
                "Manager."
            ),
            "Keywords": "PRIV mode, extended memory, TSV_TNEW_PAGE_ALLOC_FAILED, em/initial_size_MB, SM50",
        },
    },
]

_SECTION_ORDER = ["Symptom", "Environment", "Cause", "Resolution", "Keywords"]


def build_pdf(note: dict, out_path: Path) -> None:
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.set_margins(18, 16, 18)

    # Header line: "<number> - <title>", the layout the parser expects.
    pdf.set_font("Helvetica", "B", 14)
    pdf.multi_cell(0, 7, f"{note['id']} - {note['title']}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(110, 110, 110)
    pdf.multi_cell(
        0, 5, f"SAP Note {note['id']}  |  Version 3  |  Released for Customer",
        new_x="LMARGIN", new_y="NEXT",
    )
    pdf.ln(3)
    pdf.set_text_color(0, 0, 0)

    for section in _SECTION_ORDER:
        body = note["sections"].get(section)
        if not body:
            continue
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 7, section, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(0, 5.2, body, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2.5)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, "Header Data", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(
        0,
        5.2,
        f"Component: {note['component']}\nCategory: Problem\nPriority: "
        "Correction with high priority\nStatus: Released for Customer",
        new_x="LMARGIN",
        new_y="NEXT",
    )

    pdf.output(str(out_path))


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic SAP Note PDFs")
    parser.add_argument("--out", default="./data/notes")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for note in NOTES:
        path = out_dir / f"sap_note_{note['id']}.pdf"
        build_pdf(note, path)
        print(f"wrote {path}")
    print(f"\n{len(NOTES)} synthetic notes in {out_dir.resolve()}")


if __name__ == "__main__":
    main()
