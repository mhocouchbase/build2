#!/usr/bin/env python

# Copyright (c) 2010, Code Aurora Forum. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#    # Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#    # Redistributions in binary form must reproduce the above
#       copyright notice, this list of conditions and the following
#       disclaimer in the documentation and/or other materials provided
#       with the distribution.
#    # Neither the name of Code Aurora Forum, Inc. nor the names of its
#       contributors may be used to endorse or promote products derived
#       from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY EXPRESS OR IMPLIED
# WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NON-INFRINGEMENT
# ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS
# BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR
# BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE
# OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN
# IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

# This script is designed to detect when a patchset uploaded to Gerrit is
# 'identical' (determined via git-patch-id) and reapply reviews onto the new
# patchset from the previous patchset.

# Get usage and help info by running: ./trivial_rebase.py --help
# Documentation is available here: https://www.codeaurora.org/xwiki/bin/QAEP/Gerrit

import json
from optparse import OptionParser
import subprocess
from sys import exit

class CheckCallError(OSError):
  """CheckCall() returned non-0."""
  def __init__(self, command, cwd, retcode, stdout, stderr=None):
    OSError.__init__(self, command, cwd, retcode, stdout, stderr)
    self.command = command
    self.cwd = cwd
    self.retcode = retcode
    self.stdout = stdout
    self.stderr = stderr

def CheckCall(command, cwd=None):
  """Like subprocess.check_call() but returns stdout.

  Works on python 2.4
  """
  try:
    process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE)
    std_out, std_err = process.communicate()
  except OSError, e:
    raise CheckCallError(command, cwd, e.errno, None)
  if process.returncode:
    raise CheckCallError(command, cwd, process.returncode, std_out, std_err)
  return std_out, std_err

def GsqlQuery(sql_query, server, port='29418'):
  """Runs a gerrit gsql query and returns the result"""

  # Gsql *cannot* use the magic "Gerrit Code Review" user:
  # https://code.google.com/p/gerrit/issues/detail?id=2491
  # Fortunately we have another user with appropriate perms
  # and the same key
  gsql_cmd = ['ssh', '-l', 'gerrit', '-p', port, server,
              'gerrit', 'gsql', '--format',
              'JSON', '-c', sql_query]
  try:
    (gsql_out, gsql_stderr) = CheckCall(gsql_cmd)
  except CheckCallError, e:
    print "return code is %s" % e.retcode
    print "stdout and stderr is\n%s%s" % (e.stdout, e.stderr)
    raise

  new_out = gsql_out.replace('}}\n', '}}\nsplit here\n')
  return new_out.split('split here\n')

def FindPrevRev(changeId, patchset, server):
  """Finds the revision of the previous patch set on the change"""
  sql_query = ("\"SELECT revision FROM patch_sets,changes WHERE "
               "patch_sets.change_id = changes.change_id AND "
               "patch_sets.patch_set_id = %s AND "
               "changes.change_key = \'%s\'\"" % ((patchset - 1), changeId))
  revisions = GsqlQuery(sql_query, server)

  json_dict = json.loads(revisions[0], strict=False)
  return json_dict["columns"]["revision"]

def GetApprovals(changeId, patchset, server):
  """Get all the approvals on a specific patch set

  Returns a list of approval dicts"""
  sql_query = ("\"SELECT value,account_id,category_id FROM patch_set_approvals "
               "WHERE change_id = (SELECT change_id FROM changes WHERE "
               "patch_set_id = %s AND change_key = \'%s\') AND value <> 0\""
               % ((patchset - 1), changeId))
  gsql_out = GsqlQuery(sql_query, server)
  approvals = []
  for json_str in gsql_out:
    dict = json.loads(json_str, strict=False)
    if dict["type"] == "row":
      approvals.append(dict["columns"])
  return approvals

def GetEmailFromAcctId(account_id, server):
  """Returns the preferred email address associated with the account_id"""
  sql_query = ("\"SELECT preferred_email FROM accounts WHERE account_id = %s\""
               % account_id)
  email_addr = GsqlQuery(sql_query, server)

  json_dict = json.loads(email_addr[0], strict=False)
  return json_dict["columns"]["preferred_email"]

def GetPatchId(revision):
  git_show_cmd = ['git', 'show', revision]
  patch_id_cmd = ['git', 'patch-id']
  patch_id_process = subprocess.Popen(patch_id_cmd, stdout=subprocess.PIPE,
                                      stdin=subprocess.PIPE)
  git_show_process = subprocess.Popen(git_show_cmd, stdout=subprocess.PIPE)
  return patch_id_process.communicate(git_show_process.communicate()[0])[0]

def SuExec(server, port, private_key, as_user, cmd):
  # Suexec *must* use the magic "Gerrit Code Review" user.
  suexec_cmd = ['ssh', '-l', "Gerrit Code Review", '-p', port, server, '-i',
                private_key, 'suexec', '--as', as_user, '--', cmd]
  CheckCall(suexec_cmd)

def DiffCommitMessages(commit1, commit2):
  log_cmd1 = ['git', 'log', '--pretty=format:"%an %ae%n%s%n%b"',
              commit1 + '^!']
  commit1_log = CheckCall(log_cmd1)
  log_cmd2 = ['git', 'log', '--pretty=format:"%an %ae%n%s%n%b"',
              commit2 + '^!']
  commit2_log = CheckCall(log_cmd2)
  if commit1_log != commit2_log:
    return True
  return False

def Main():
  server = 'localhost'
  usage = "usage: %prog <required options> [--server-port=PORT]"
  parser = OptionParser(usage=usage)
  parser.add_option("--change", dest="changeId", help="Change identifier")
  parser.add_option("--project", help="Project path in Gerrit")
  parser.add_option("--branch", help="[ignored]")
  parser.add_option("--change-url", help="[ignored]")
  parser.add_option("--commit", help="Git commit-ish for this patchset")
  parser.add_option("--patchset", type="int", help="The patchset number")
  parser.add_option("--private-key-path", dest="private_key_path",
                    help="Full path to Gerrit SSH daemon's private host key")
  parser.add_option("--server-port", dest="port", default='29418',
                    help="Port to connect to Gerrit's SSH daemon "
                         "[default: %default]")

  (options, args) = parser.parse_args()

  if not options.changeId:
    parser.print_help()
    exit(0)

  if options.patchset == 1:
    # Nothing to detect on first patchset
    print "First patchset, no need to check for rebase"
    exit(0)
  prev_revision = None
  prev_revision = FindPrevRev(options.changeId, options.patchset, server)
  if not prev_revision:
    # Couldn't find a previous revision
    print "Couldn't find previous revision?"
    exit(0)
  prev_patch_id = GetPatchId(prev_revision)
  cur_patch_id = GetPatchId(options.commit)
  if cur_patch_id.split()[0] != prev_patch_id.split()[0]:
    # patch-ids don't match
    print "trivial-rebase: patch_ids don't match"
    exit(0)
  # Patch ids match. This is a trivial rebase.
  print "Trivial rebase detected!"
  # In addition to patch-id we should check if the commit message changed. Most
  # approvers would want to re-review changes when the commit message changes.
  changed = DiffCommitMessages(prev_revision, options.commit)
  if changed:
    # Insert a comment into the change letting the approvers know only the
    # commit message changed
    print "Commit message changed, filing comment"
    comment_msg = ("\'--message=New patchset patch-id matches previous patchset"
                   ", but commit message has changed.'")
    comment_cmd = ['ssh', '-p', options.port, server, 'gerrit', 'review',
                   '--project', options.project, comment_msg, options.commit]
    CheckCall(comment_cmd)
    exit(0)

  # Need to get all approvals on prior patch set, then suexec them onto
  # this patchset.
  approvals = GetApprovals(options.changeId, options.patchset, server)
  gerrit_approve_msg = ("\'Automatically re-added by Gerrit trivial rebase "
                        "detection script.\'")
  for approval in approvals:
    # Note: Sites with different 'copy_min_score' values in the
    # approval_categories DB table might want different behavior here.
    # Additional categories should also be added if desired.
    if approval["category_id"] == "Code-Review":
      approve_category = '--code-review'
    elif approval["category_id"] == "Verified":
      # Don't re-add verifies
      #approve_category = '--verified'
      continue
    elif approval["category_id"] == "SUBM":
      # We don't care about previous submit attempts
      continue
    else:
      print "Unsupported category: %s" % approval
      exit(0)

    score = approval["value"]
    gerrit_approve_cmd = ['gerrit', 'review', '--project', options.project,
                          '--message', gerrit_approve_msg, approve_category,
                          score, options.commit]
    print "About to approve..."
    email_addr = GetEmailFromAcctId(approval["account_id"], server)
    print "Email address: {}".format(email_addr)
    SuExec(server, options.port, options.private_key_path, email_addr,
           ' '.join(gerrit_approve_cmd))
  exit(0)

if __name__ == "__main__":
  print "Executing trivial-rebase check..."
  Main()
  print "trivial-rebase check complete."
