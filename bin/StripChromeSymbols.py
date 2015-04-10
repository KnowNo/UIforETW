# Copyright 2015 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
This script exists to work around severe performane problems when WPA or other
Windows Performance Toolkit programs try to load the symbols for the Chrome
web browser. Some combination of the enormous size of the symbols or the
enhanced debug information generated by /Zo causes WPA to take about twenty
minutes to process the symbols for chrome.dll and chrome_child.dll. When
profiling Chrome this delay happens with every new set of symbols, so with
every new version of Chrome.

This script uses xperf actions to dump a list of the symbols referenced in
an ETW trace. If chrome.dll or chrome_child.dll is detected and if decoded
symbols are not found in %_NT_SYMCACHE_PATH% (default is c:\symcache) then
RetrieveSymbols.exe is used to download the symbols from the Chromium
symbol server, pdbcopy.exe is used to strip the private symbols, and then
another xperf action is used to load the stripped symbols, thus converting
them to .symcache files that can be efficiently loaded by WPA.

More details on the discovery of this slowness and the evolution of the fix
can be found here:
https://randomascii.wordpress.com/2014/11/04/slow-symbol-loading-in-microsofts-profiler-take-two/

Discussion and source code for RetrieveSymbols.exe can be found here:
https://randomascii.wordpress.com/2013/03/09/symbols-the-microsoft-way/

If "chromium-browser-symsrv" is not found in _NT_SYMBOL_PATH or RetrieveSymbols.exe
and pdbcopy.exe are not found then this script will exit early.
"""

import os
import sys
import re
import tempfile
import shutil

if len(sys.argv) < 2:
  print "Usage: %s trace.etl" % sys.argv[0]
  sys.exit(0)

symbolPath = os.environ.get("_NT_SYMBOL_PATH", "")
if symbolPath.count("chromium-browser-symsrv") == 0:
  print "Chromium symbol server is not in _NT_SYMBOL_PATH. No symbol stripping needed."
  sys.exit(0)

scriptDir = os.path.split(sys.argv[0])[0]
retrievePath = os.path.join(scriptDir, "RetrieveSymbols.exe")
pdbcopyPath = os.path.join(scriptDir, "pdbcopy.exe")

# RetrieveSymbols.exe requires some support files. dbghelp.dll and symsrv.dll
# have to be in the same directory as RetrieveSymbols.exe and pdbcopy.exe must
# be in the path, so copy them all to the script directory.
for third_party in ["pdbcopy.exe", "dbghelp.dll", "symsrv.dll"]:
  if not os.path.exists(third_party):
    source = os.path.normpath(os.path.join(scriptDir, r"..\third_party", \
        third_party))
    dest = os.path.normpath(os.path.join(scriptDir, third_party))
    shutil.copy2(source, dest)

if not os.path.exists(pdbcopyPath):
  print "pdbcopy.exe not found. No symbol stripping is possible."
  sys.exit(0)

if not os.path.exists(retrievePath):
  print "RetrieveSymbols.exe not found. No symbol retrieval is possible."
  sys.exit(0)

tracename = sys.argv[1]
# Each symbol file that we pdbcopy gets copied to a separate directory so
# that we can support decoding symbols for multiple chrome versions without
# filename collisions.
tempdirs = []

# Typical output looks like:
# "[RSDS] PdbSig: {be90dbc6-fe31-4842-9c72-7e2ea88f0adf}; Age: 1; Pdb: C:\b\build\slave\win\build\src\out\Release\syzygy\chrome.dll.pdb"
pdbRe = re.compile(r'"\[RSDS\] PdbSig: {(.*-.*-.*-.*-.*)}; Age: (.*); Pdb: (.*)"')
pdbCachedRe = re.compile(r"Found symbol file - placed it in (.*)")

print "Pre-translating chrome symbols from stripped PDBs to avoid 10-15 minute translation times."

symcacheFiles = []
# Keep track of the local symbol files so that we can temporarily rename them
# to stop xperf from using -- rename them from .pdb to .pdbx
localSymbolFiles = []

command = 'xperf -i "%s" -tle -tti -a symcache -dbgid' % tracename
print "> %s" % command
foundUncached = False
for line in os.popen(command).readlines():
  if line.count("chrome.dll") > 0 or line.count("chrome_child.dll") > 0:
    match = pdbRe.match(line)
    if match:
      guid, age, path = match.groups()
      guid = guid.replace("-", "")
      filepart = os.path.split(path)[1]
      symcacheFile = r"c:\symcache\chrome.dll-%s%sv2.symcache" % (guid, age)
      if os.path.exists(symcacheFile):
        #print "Symcache file %s already exists. Skipping." % symcacheFile
        continue
      # Only print messages for chrome PDBs that aren't in the symcache
      foundUncached = True
      print "Found uncached reference to %s: %s - %s" % (filepart, guid, age, )
      symcacheFiles.append(symcacheFile)
      pdbCachePath = None
      retrieveCommand = "%s %s %s %s" % (retrievePath, guid, age, filepart)
      print "> %s" % retrieveCommand
      for subline in os.popen(retrieveCommand):
        print subline.strip()
        cacheMatch = pdbCachedRe.match(subline.strip())
        if cacheMatch:
          pdbCachePath = cacheMatch.groups()[0]
      if not pdbCachePath:
        # Look for locally built symbols
        if os.path.exists(path):
          pdbCachePath = path
          localSymbolFiles.append(path)
      if pdbCachePath:
        tempdir = tempfile.mkdtemp()
        tempdirs.append(tempdir)
        destPath = os.path.join(tempdir, os.path.split(pdbCachePath)[1])
        print "Copying PDB to %s" % destPath
        for copyline in os.popen("%s %s %s -p" % (pdbcopyPath, pdbCachePath, destPath)):
          print copyline.strip()
      else:
        print "Failed to retrieve symbols. Check for RetrieveSymbols.exe and support files."

if tempdirs:
  symbolPath = ";".join(tempdirs)
  print "Stripped PDBs are in %s. Converting to symcache files now." % symbolPath
  os.environ["_NT_SYMBOL_PATH"] = symbolPath
  for localPDB in localSymbolFiles:
    tempName = localPDB + "x"
    print "Renaming %s to %s to stop unstripped PDBs from being used." % (localPDB, tempName)
    os.rename(localPDB, tempName)
  genCommand = 'xperf -i "%s" -symbols -tle -tti -a symcache -build' % tracename
  print "> %s" % genCommand
  for line in os.popen(genCommand).readlines():
    pass # Don't print line
  for localPDB in localSymbolFiles:
    tempName = localPDB + "x"
    os.rename(tempName, localPDB)
  error = False
  for symcacheFile in symcacheFiles:
    if os.path.exists(symcacheFile):
      print "%s generated." % symcacheFile
    else:
      print "Error: %s not generated." % symcacheFile
      error = True
  # Delete the stripped PDB files
  if error:
    print "Retaining PDBs to allow rerunning xperf command-line."
  else:
    for dir in tempdirs:
      shutil.rmtree(dir, ignore_errors=True)
else:
  if foundUncached:
    print "No PDBs copied, nothing to do."
  else:
    print "No uncached PDBS found, nothing to do."
