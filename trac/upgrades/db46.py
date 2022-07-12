# -*- coding: utf-8 -*-
#
# Copyright (C) 2022 Edgewall Software
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution. The terms
# are also available at https://trac.edgewall.org/wiki/TracLicense.
#
# This software consists of voluntary contributions made by many
# individuals. For the exact contribution history, see the revision
# history and logs, available at https://trac.edgewall.org/.

sql = [
#-- Add 'started' value to 'milestone'
"""CREATE TEMPORARY TABLE milestone_old AS SELECT * FROM milestone;""",
"""DROP TABLE milestone;""",
"""CREATE TABLE milestone (
	name	text,
	due	integer,
	started	integer,
	completed	integer,
	description	text,
	PRIMARY KEY(name)
);""",
"""INSERT INTO milestone(name,due,started,completed,description) SELECT name,due,0,completed,description FROM milestone_old;"""
]

def do_upgrade(env, ver, cursor):
    cursor.execute(sql)