"""Exact namespace filtering plus a real owned local/CI citext archive restore."""
import os,re,shutil,subprocess,sys,uuid
from pathlib import Path
import pytest
from sqlalchemy import create_engine,text
from sqlalchemy.exc import IntegrityError
from flavor_pg_fixture import explorer_pg_base,admitted_test_endpoint
from scripts.development import product_explorer_release_worker as worker

def test_namespace_filter_removes_exactly_one_public_definition_and_preserves_all_other_bytes():
    toc=("; archive header\n2; 3079 16401 EXTENSION - citext owner\n"
         "3; 2615 2200 SCHEMA - public owner\n4; 1259 16402 TABLE public synthetic owner\n"
         "5; 0 16402 TABLE DATA public synthetic owner\n6; 2606 16403 CONSTRAINT public synthetic unique owner\n")
    filtered,proof=worker.restore_namespace_list(toc)
    omitted="3; 2615 2200 SCHEMA - public owner\n"
    assert filtered==toc.replace(omitted,"")
    assert proof["omitted_count"]==1 and proof["omitted_entry_sha256"]==worker.sha(omitted.encode())
    assert proof["original_toc_sha256"]==worker.sha(toc.encode())
    assert proof["filtered_toc_sha256"]==worker.sha(filtered.encode())
    for invalid in (toc.replace(omitted,""),toc+omitted,toc.replace("SCHEMA - public owner","SCHEMA - publicity owner"),toc+"\x00"):
        with pytest.raises(AssertionError):worker.restore_namespace_list(invalid)

def test_native_citext_archive_keeps_owned_empty_public_and_restores_all_other_entries(explorer_pg_base,tmp_path):
    admin=explorer_pg_base
    assert admitted_test_endpoint(admin.url)
    if sys.platform=="darwin":
        assert admin.url.port==55486 and os.environ.get("AVA_BOUNDED_SESSION","").startswith("AVA-ERUPTED-")
        clients=Path("/opt/homebrew/opt/postgresql@16/bin")
        pg_dump=str(clients/"pg_dump");pg_restore=str(clients/"pg_restore")
        with admin.connect() as c:
            directory=Path(c.scalar(text("SHOW data_directory"))).resolve()
            assert directory.is_relative_to(Path(os.environ["TMPDIR"]).resolve())
            assert int(c.scalar(text("SHOW server_version_num")))//10000==16
    else:
        assert sys.platform=="linux" and admin.url.port==5433 and admin.url.host=="127.0.0.1"
        assert os.getuid()==2001 and Path.home()==Path("/home/ci-tests") and os.getenv("ENVIRONMENT")=="test"
        assert not os.getenv("AVA_BOUNDED_SESSION") and tmp_path.resolve().is_relative_to(Path("/tmp"))
        pg_dump=shutil.which("pg_dump");pg_restore=shutil.which("pg_restore")
        assert pg_dump and pg_restore
        assert Path(pg_dump).parent==Path("/tmp/erupted-ci-bin")==Path(pg_restore).parent
        with admin.connect() as c:assert c.scalar(text("SHOW server_version")).startswith("18.6")
    suffix=uuid.uuid4().hex
    role="explorer_archive_"+suffix
    source="explorer_archive_source_"+suffix;target="explorer_archive_target_"+suffix
    assert all(re.fullmatch(r"explorer_archive_(?:source_|target_)?[a-f0-9]{32}",v) for v in (role,source,target))
    engines=[];made=[]
    tmp_path.chmod(0o755)  # Only synthetic archive bytes; Docker clients share /tmp.
    archive=tmp_path/"synthetic.dump"
    env=dict(os.environ,PGPASSWORD="ci_only")
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            c.exec_driver_sql('CREATE ROLE "'+role+'" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD \'ci_only\'')
            made.append(role)
            for database in (source,target):
                c.exec_driver_sql('CREATE DATABASE "'+database+'" OWNER "'+role+'"');made.append(database)
        source_engine=create_engine(admin.url.set(username=role,password="ci_only",database=source))
        target_engine=create_engine(admin.url.set(username=role,password="ci_only",database=target))
        engines=[source_engine,target_engine]
        with source_engine.begin() as c:
            c.exec_driver_sql('ALTER SCHEMA public OWNER TO "'+role+'"')
            c.exec_driver_sql("CREATE EXTENSION citext WITH SCHEMA public")
            c.exec_driver_sql("CREATE TABLE public.synthetic_archive(id BIGINT PRIMARY KEY,label public.citext UNIQUE NOT NULL)")
            c.exec_driver_sql("INSERT INTO public.synthetic_archive VALUES(1,'Example'),(2,'Other')")
            before=worker.fingerprints(c,[("public","synthetic_archive")])
        args=["--host",admin.url.host,"--port",str(admin.url.port),"--username",role]
        with archive.open("wb") as handle:
            result=subprocess.run([pg_dump,"--format=custom","--no-owner","--no-acl",*args,"--dbname",source],
                env=env,stdout=handle,stderr=subprocess.PIPE,timeout=30)
        assert result.returncode==0,result.stderr.decode("utf8","replace")
        archive.chmod(0o644)
        toc=subprocess.check_output([pg_restore,"--list",str(archive)],env=env,text=True,timeout=20)
        # Model the production ordering even if a client version emitted SCHEMA first.
        original_lines=toc.splitlines(keepends=True)
        namespace=[line for line in original_lines if re.fullmatch(
            r"[0-9]+;\s+[0-9]+\s+[0-9]+\s+SCHEMA\s+-\s+public\s+[^\r\n]+[\r\n]*",line)]
        assert len(namespace)==1
        lines=[line for line in original_lines if line!=namespace[0]]
        extension=next(i for i,line in enumerate(lines) if re.search(r"\sEXTENSION\s+-\s+citext\s",line))
        lines.insert(extension+1,namespace[0])
        ordered="".join(lines)
        filtered,proof=worker.restore_namespace_list(ordered)
        assert filtered=="".join(line for line in original_lines if line!=namespace[0])
        assert "EXTENSION" in filtered and "TABLE DATA" in filtered and "CONSTRAINT" in filtered
        with target_engine.begin() as c:
            empty=worker.verify_empty_owned_public(c,target,role)
            assert empty["user_objects"]==0 and empty["default_namespace_preserved"]
        result=subprocess.run([pg_restore,"--exit-on-error","--single-transaction","--no-owner","--no-acl",
            "--use-list=/dev/stdin",*args,"--dbname",target,str(archive)],env=env,input=filtered.encode(),
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
        assert result.returncode==0,result.stderr.decode("utf8","replace")
        with target_engine.connect() as c:
            assert worker.fingerprints(c,[("public","synthetic_archive")])==before
            assert c.scalar(text("SELECT count(*) FROM public.synthetic_archive WHERE label='EXAMPLE'"))==1
            assert c.scalar(text("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database()"))==role
            assert not c.scalar(text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user"))
            assert c.scalar(text("SELECT extnamespace='public'::regnamespace FROM pg_extension WHERE extname='citext'"))
        with pytest.raises(IntegrityError):
            with target_engine.begin() as c:c.exec_driver_sql("INSERT INTO public.synthetic_archive VALUES(3,'eXaMpLe')")
        assert proof["omitted_count"]==1
    finally:
        for engine in engines:engine.dispose()
        archive.unlink(missing_ok=True)
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            for database in (source,target):
                if database in made:
                    c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:name"),{"name":database})
                    c.exec_driver_sql('DROP DATABASE "'+database+'"')
            if role in made:c.exec_driver_sql('DROP ROLE "'+role+'"')
