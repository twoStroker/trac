# -*- coding: utf-8 -*-
#
# Copyright (C) 2005-2022 Edgewall Software
# Copyright (C) 2005-2006 Christopher Lenz <cmlenz@gmx.de>
# Copyright (C) 2005 Jeff Weiss <trac@jeffweiss.org>
# Copyright (C) 2006 Andres Salomon <dilinger@athenacr.com>
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution. The terms
# are also available at https://trac.edgewall.org/wiki/TracLicense.
#
# This software consists of voluntary contributions made by many
# individuals. For the exact contribution history, see the revision
# history and logs, available at https://trac.edgewall.org/log/.

import os
import re
import sys
from contextlib import closing
from subprocess import Popen, PIPE

from trac.api import IEnvironmentSetupParticipant
from trac.core import *
from trac.config import Option
from trac.db.api import ConnectionBase, DatabaseManager, IDatabaseConnector, \
                        get_column_names, parse_connection_uri
from trac.db.util import ConnectionWrapper, IterableCursor
from trac.util import as_int, get_pkginfo
from trac.util.html import Markup
from trac.util.compat import close_fds
from trac.util.text import exception_to_unicode, to_unicode
from trac.util.translation import _

_like_escape_re = re.compile(r'([/_%])')

try:
    import pymysql
except ImportError:
    pymysql = None
    pymsql_version = None
else:
    pymsql_version = get_pkginfo(pymysql).get('version', pymysql.__version__)

    class MySQLUnicodeCursor(pymysql.cursors.Cursor):
        def execute(self, query, args=None):
            if args:
                args = tuple(str(arg) if isinstance(arg, Markup) else arg
                             for arg in args)
            return super().execute(query, args)

        def executemany(self, query, args):
            if args:
                args = [tuple(str(item) if isinstance(item, Markup) else item
                              for item in arg)
                        for arg in args]
            return super().executemany(query, args)

        def fetchall(self):
            return list(super().fetchall())

    class MySQLSilentCursor(MySQLUnicodeCursor):
        def _show_warnings(self, conn=None):
            pass


# Mapping from "abstract" SQL types to DB-specific types
_type_map = {
    'int64': 'bigint',
    'text': 'mediumtext',
}


def _quote(identifier):
    return "`%s`" % identifier.replace('`', '``')


class MySQLConnector(Component):
    """Database connector for MySQL version 4.1 and greater.

    Database URLs should be of the form::

      {{{
      mysql://user[:password]@host[:port]/database[?param1=value&param2=value]
      }}}

    The following parameters are supported:
     * `compress`: Enable compression (0 or 1)
     * `init_command`: Command to run once the connection is created
     * `named_pipe`: Use a named pipe to connect on Windows (0 or 1)
     * `read_default_file`: Read default client values from the given file
     * `read_default_group`: Configuration group to use from the default file
     * `unix_socket`: Use a Unix socket at the given path to connect
    """
    implements(IDatabaseConnector, IEnvironmentSetupParticipant)

    required = False

    mysqldump_path = Option('trac', 'mysqldump_path', 'mysqldump',
        """Location of mysqldump for MySQL database backups""")

    def __init__(self):
        if pymysql:
            self._mysql_version = \
                'server: (not-connected), client: "%s", thread-safe: %s' % \
                (pymysql.get_client_info(), pymysql.thread_safe())
        else:
            self._mysql_version = None

    # IDatabaseConnector methods

    def get_supported_schemes(self):
        yield 'mysql', 1

    def get_connection(self, path, log=None, user=None, password=None,
                       host=None, port=None, params={}):
        cnx = MySQLConnection(path, log, user, password, host, port, params)
        if not self.required:
            self._mysql_version = \
                'server: "%s", client: "%s", thread-safe: %s' \
                % (cnx.cnx.get_server_info(), pymysql.get_client_info(),
                   pymysql.thread_safe())
            self.required = True
        return cnx

    def get_exceptions(self):
        return pymysql

    def init_db(self, path, schema=None, log=None, user=None, password=None,
                host=None, port=None, params={}):
        cnx = self.get_connection(path, log, user, password, host, port,
                                  params)
        self._verify_variables(cnx)
        max_bytes = self._max_bytes(cnx)
        cursor = cnx.cursor()
        if schema is None:
            from trac.db_default import schema
        for table in schema:
            for stmt in self.to_sql(table, max_bytes=max_bytes):
                self.log.debug(stmt)
                cursor.execute(stmt)
        self._verify_table_status(cnx)
        cnx.commit()

    def destroy_db(self, path, log=None, user=None, password=None, host=None,
                   port=None, params={}):
        cnx = self.get_connection(path, log, user, password, host, port,
                                  params)
        for table_name in cnx.get_table_names():
            cnx.drop_table(table_name)
        cnx.commit()

    def db_exists(self, path, log=None, user=None, password=None, host=None,
                  port=None, params={}):
        cnx = self.get_connection(path, log, user, password, host, port,
                                  params)
        return bool(cnx.get_table_names())

    def _max_bytes(self, cnx):
        if cnx is None:
            connector, args = DatabaseManager(self.env).get_connector()
            with closing(connector.get_connection(**args)) as cnx:
                charset = cnx.charset
        else:
            charset = cnx.charset
        return 4 if charset == 'utf8mb4' else 3

    _max_key_length = 3072

    def _collist(self, table, columns, max_bytes):
        """Take a list of columns and impose limits on each so that indexing
        works properly.

        Some Versions of MySQL limit each index prefix to 3072 bytes total,
        with a max of 767 bytes per column.
        """
        cols = []
        limit_col = 767 // max_bytes
        limit = min(self._max_key_length // (max_bytes * len(columns)),
                    limit_col)
        for c in columns:
            name = _quote(c)
            table_col = list(filter((lambda x: x.name == c), table.columns))
            if len(table_col) == 1 and table_col[0].type.lower() == 'text':
                if table_col[0].key_size is not None:
                    name += '(%d)' % min(table_col[0].key_size, limit_col)
                else:
                    name += '(%s)' % limit
            # For non-text columns, we simply throw away the extra bytes.
            # That could certainly be optimized better, but for now let's KISS.
            cols.append(name)
        return ','.join(cols)

    def to_sql(self, table, max_bytes=None):
        if max_bytes is None:
            max_bytes = self._max_bytes(None)
        sql = ['CREATE TABLE %s (' % _quote(table.name)]
        coldefs = []
        for column in table.columns:
            ctype = column.type
            ctype = _type_map.get(ctype, ctype)
            if column.auto_increment:
                ctype = 'INT UNSIGNED NOT NULL AUTO_INCREMENT'
                # Override the column type, as a text field cannot
                # use auto_increment.
                column.type = 'int'
            coldefs.append('    %s %s' % (_quote(column.name), ctype))
        if len(table.key) > 0:
            coldefs.append('    PRIMARY KEY (%s)' %
                           self._collist(table, table.key,
                                         max_bytes=max_bytes))
        sql.append(',\n'.join(coldefs) + '\n)')
        yield '\n'.join(sql)

        for index in table.indices:
            unique = 'UNIQUE' if index.unique else ''
            idxname = '%s_%s_idx' % (table.name, '_'.join(index.columns))
            yield 'CREATE %s INDEX %s ON %s (%s)' % \
                  (unique, _quote(idxname), _quote(table.name),
                   self._collist(table, index.columns, max_bytes=max_bytes))

    def alter_column_types(self, table, columns):
        """Yield SQL statements altering the type of one or more columns of
        a table.

        Type changes are specified as a `columns` dict mapping column names
        to `(from, to)` SQL type tuples.
        """
        alterations = []
        for name, (from_, to) in sorted(columns.items()):
            to = _type_map.get(to, to)
            if to != _type_map.get(from_, from_):
                alterations.append((name, to))
        if alterations:
            yield "ALTER TABLE %s %s" % (table,
                ', '.join("MODIFY %s %s" % each
                          for each in alterations))

    def backup(self, dest_file):
        db_url = self.env.config.get('trac', 'database')
        scheme, db_prop = parse_connection_uri(db_url)
        db_params = db_prop.setdefault('params', {})
        db_name = os.path.basename(db_prop['path'])

        args = [self.mysqldump_path, '--no-defaults']
        if 'host' in db_prop:
            args.extend(['-h', db_prop['host']])
        if 'port' in db_prop:
            args.extend(['-P', str(db_prop['port'])])
        if 'user' in db_prop:
            args.extend(['-u', db_prop['user']])
        for name, value in db_params.items():
            if name == 'compress' and as_int(value, 0):
                args.append('--compress')
            elif name == 'named_pipe' and as_int(value, 0):
                args.append('--protocol=pipe')
            elif name == 'read_default_file':  # Must be first
                args.insert(1, '--defaults-file=' + value)
            elif name == 'unix_socket':
                args.extend(['--protocol=socket', '--socket=' + value])
            elif name not in ('init_command', 'read_default_group'):
                self.log.warning("Invalid connection string parameter '%s'",
                                 name)
        args.extend(['-r', dest_file, db_name])

        environ = os.environ.copy()
        if 'password' in db_prop:
            environ['MYSQL_PWD'] = str(db_prop['password'])
        try:
            p = Popen(args, env=environ, stderr=PIPE, close_fds=close_fds)
        except OSError as e:
            raise TracError(_("Unable to run %(path)s: %(msg)s",
                              path=self.mysqldump_path,
                              msg=exception_to_unicode(e)))
        errmsg = p.communicate()[1]
        if p.returncode != 0:
            raise TracError(_("mysqldump failed: %(msg)s",
                              msg=to_unicode(errmsg.strip())))
        if not os.path.exists(dest_file):
            raise TracError(_("No destination file created"))
        return dest_file

    def get_system_info(self):
        yield 'MySQL', self._mysql_version
        yield pymysql.__name__, pymsql_version

    # IEnvironmentSetupParticipant methods

    def environment_created(self):
        pass

    def environment_needs_upgrade(self):
        if self.required:
            with self.env.db_query as db:
                self._verify_table_status(db)
                self._verify_variables(db)
        return False

    def upgrade_environment(self):
        pass

    UNSUPPORTED_ENGINES = ('MyISAM', 'EXAMPLE', 'ARCHIVE', 'CSV', 'ISAM')

    def _verify_table_status(self, db):
        from trac.db_default import schema
        tables = [t.name for t in schema]
        cursor = db.cursor()
        cursor.execute("SHOW TABLE STATUS WHERE name IN (%s)" %
                       ','.join(('%s',) * len(tables)),
                       tables)
        cols = get_column_names(cursor)
        rows = [dict(zip(cols, row)) for row in cursor]

        engines = [row['Name'] for row in rows
                               if row['Engine'] in self.UNSUPPORTED_ENGINES]
        if engines:
            raise TracError(_(
                "All tables must be created as InnoDB or NDB storage engine "
                "to support transactions. The following tables have been "
                "created as storage engine which doesn't support "
                "transactions: %(tables)s", tables=', '.join(engines)))

        non_utf8bin = [row['Name'] for row in rows
                       if row['Collation'] not in ('utf8_bin', 'utf8mb4_bin',
                                                   None)]
        if non_utf8bin:
            raise TracError(_("All tables must be created with utf8_bin or "
                              "utf8mb4_bin as collation. The following tables "
                              "don't have the collations: %(tables)s",
                              tables=', '.join(non_utf8bin)))

    SUPPORTED_COLLATIONS = (
        ('utf8mb4', 'utf8mb4_bin'),
        ('utf8mb3', 'utf8_bin'),
        ('utf8', 'utf8_bin'),
    )

    def _verify_variables(self, db):
        cursor = db.cursor()
        cursor.execute("SHOW VARIABLES WHERE variable_name IN ("
                       "'default_storage_engine','storage_engine',"
                       "'default_tmp_storage_engine',"
                       "'character_set_database','collation_database')")
        vars = {row[0].lower(): row[1] for row in cursor}

        engine = vars.get('default_storage_engine') or \
                 vars.get('storage_engine')
        if engine in self.UNSUPPORTED_ENGINES:
            raise TracError(_("The current storage engine is %(engine)s. "
                              "It must be InnoDB or NDB storage engine to "
                              "support transactions.", engine=engine))

        tmp_engine = vars.get('default_tmp_storage_engine')
        if tmp_engine in self.UNSUPPORTED_ENGINES:
            raise TracError(_("The current storage engine for TEMPORARY "
                              "tables is %(engine)s. It must be InnoDB or NDB "
                              "storage engine to support transactions.",
                              engine=tmp_engine))

        charset = vars['character_set_database']
        collation = vars['collation_database']
        if (charset, collation) not in self.SUPPORTED_COLLATIONS:
            raise TracError(_(
                "The charset and collation of database are '%(charset)s' and "
                "'%(collation)s'. The database must be created with one of "
                "%(supported)s.", charset=charset, collation=collation,
                supported=repr(self.SUPPORTED_COLLATIONS)))


class MySQLConnection(ConnectionBase, ConnectionWrapper):
    """Connection wrapper for MySQL."""

    poolable = True

    def __init__(self, path, log, user=None, password=None, host=None,
                 port=None, params={}):
        if path.startswith('/'):
            path = path[1:]
        if password is None:
            password = ''
        if port is None:
            port = 3306
        opts = {'charset': 'utf8'}
        for name, value in params.items():
            if name == 'read_default_group':
                opts[name] = value
            elif name == 'init_command':
                opts[name] = value
            elif name in ('read_default_file', 'unix_socket'):
                opts[name] = value
            elif name in ('compress', 'named_pipe'):
                opts[name] = as_int(value, 0)
            elif name == 'charset':
                value = value.lower()
                if value in ('utf8', 'utf8mb4'):
                    opts[name] = value
                else:
                    self.log.warning("Invalid connection string parameter "
                                     "'%s=%s'", name, value)
            else:
                self.log.warning("Invalid connection string parameter '%s'",
                                 name)
        cnx = pymysql.connect(db=path, user=user, passwd=password, host=host,
                              port=port, **opts)
        cursor = cnx.cursor()
        cursor.execute("SHOW VARIABLES WHERE "
                       " variable_name='character_set_database'")
        charset = cursor.fetchone()[1]
        if charset == 'utf8mb3':
            charset = 'utf8'
        self.charset = charset
        cursor.close()
        if self.charset != opts['charset']:
            cnx.close()
            opts['charset'] = self.charset
            cnx = pymysql.connect(db=path, user=user, passwd=password,
                                  host=host, port=port, **opts)
        self.schema = path
        ConnectionWrapper.__init__(self, cnx, log)
        self._is_closed = False

    def cursor(self):
        return IterableCursor(MySQLUnicodeCursor(self.cnx), self.log)

    def rollback(self):
        self.cnx.ping()
        try:
            self.cnx.rollback()
        except pymysql.ProgrammingError:
            self._is_closed = True

    def close(self):
        if not self._is_closed:
            try:
                self.cnx.close()
            except pymysql.ProgrammingError:
                pass # this error would mean it's already closed.  So, ignore
            self._is_closed = True

    def cast(self, column, type):
        if type in ('int', 'int64'):
            type = 'signed'
        elif type == 'text':
            type = 'char'
        return 'CAST(%s AS %s)' % (column, type)

    def concat(self, *args):
        return 'concat(%s)' % ', '.join(args)

    def drop_column(self, table, column):
        cursor = pymysql.cursors.Cursor(self.cnx)
        if column in self.get_column_names(table):
            quoted_table = self.quote(table)
            cursor.execute("SHOW INDEX FROM %s" % quoted_table)
            columns = get_column_names(cursor)
            keys = {}
            for row in cursor.fetchall():
                row = dict(zip(columns, row))
                keys.setdefault(row['Key_name'], []).append(row['Column_name'])
            # drop all composite indices which in the given column is involved
            for key, columns in keys.items():
                if len(columns) > 1 and column in columns:
                    if key == 'PRIMARY':
                        cursor.execute("ALTER TABLE %s DROP PRIMARY KEY" %
                                       quoted_table)
                    else:
                        cursor.execute("ALTER TABLE %s DROP KEY %s" %
                                       (quoted_table, self.quote(key)))
            cursor.execute("ALTER TABLE %s DROP COLUMN %s " %
                           (quoted_table, self.quote(column)))

    def drop_table(self, table):
        cursor = MySQLSilentCursor(self.cnx)
        cursor.execute("DROP TABLE IF EXISTS " + self.quote(table))

    def get_column_names(self, table):
        rows = self.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema=%s AND table_name=%s
            ORDER BY ordinal_position
            """, (self.schema, table))
        return [row[0] for row in rows]

    def get_last_id(self, cursor, table, column='id'):
        return cursor.lastrowid

    def get_sequence_names(self):
        return []

    def get_table_names(self):
        rows = self.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema=%s
            """, (self.schema,))
        return [row[0] for row in rows]

    def has_table(self, table):
        rows = self.execute("""
            SELECT EXISTS (SELECT * FROM information_schema.columns
                           WHERE table_schema=%s AND table_name=%s)
            """, (self.schema, table))
        return bool(rows[0][0])

    def like(self):
        return "LIKE %%s COLLATE %s_general_ci ESCAPE '/'" % self.charset

    def like_escape(self, text):
        return _like_escape_re.sub(r'/\1', text)

    def reset_tables(self):
        table_names = []
        if not self.schema:
            return table_names
        cursor = self.cursor()
        cursor.execute("""
            SELECT t.table_name,
                   EXISTS (SELECT * FROM information_schema.columns AS c
                           WHERE c.table_schema=t.table_schema
                           AND c.table_name=t.table_name
                           AND extra='auto_increment')
            FROM information_schema.tables AS t
            WHERE t.table_schema=%s
            """, (self.schema,))
        for table, has_autoinc in cursor.fetchall():
            table_names.append(table)
            quoted = self.quote(table)
            if not has_autoinc:
                # DELETE FROM is preferred to TRUNCATE TABLE, as the
                # auto_increment is not used.
                cursor.execute("DELETE FROM %s" % quoted)
            else:
                # TRUNCATE TABLE is preferred to DELETE FROM, as we
                # need to reset the auto_increment in MySQL.
                cursor.execute("TRUNCATE TABLE %s" % quoted)
        return table_names

    def prefix_match(self):
        return "LIKE %s ESCAPE '/'"

    def prefix_match_value(self, prefix):
        return self.like_escape(prefix) + '%'

    def quote(self, identifier):
        """Return the quoted identifier."""
        return _quote(identifier)

    def update_sequence(self, cursor, table, column='id'):
        # MySQL handles sequence updates automagically
        pass
