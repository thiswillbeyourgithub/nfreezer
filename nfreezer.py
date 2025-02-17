"""
nFreezer is an encrypted-at-rest backup tool.

Homepage and documentation: https://github.com/josephernest/nfreezer

Copyright (c) 2020, Joseph Ernest. See also LICENSE file.

==TODO==
* implement a logfile

* when backing up, use compiled regexp for the exclusion list
* move decrypt flist thread at the top
* use a pandas dataframe for .files, export as json (with values encrypted of course) every 10s instead of flist.write() at each turn
* ability to sort by modification time when restoring or backing up
* switch from pysftp to paramiko, as the former is abandonned (security risk?)
* respect PEP8, as keeping the number of lines small at the cost of coding
    conventions doesn't make much sense
* investigate how to implement incremental backups
"""

import pysftp, getpass, paramiko, glob, os, hashlib, io, Crypto.Random, Crypto.Protocol.KDF, Crypto.Cipher.AES, uuid, zlib, time, pprint, sys, contextlib, threading, re
from tqdm import tqdm

NULL16BYTES, NULL32BYTES = b'\x00' * 16, b'\x00' * 32
BLOCKSIZE = 16*1024*1024  # 8 MB
larger_files_first = True
MAX_THREADS = 4
SMALL_FILE = 1048576 # 1024*1024*1  # threadings skips smaller files
red = "\033[91m"
yel = "\033[93m"
rst = "\033[0m"  # reset color

@contextlib.contextmanager  
def nullcontext():  # from contextlib import nullcontext for Python 3.7+
    yield None

def get_size(path):
    try: return os.path.getsize(path)
    except FileNotFoundError: return 4096

def getsha256(f):
    sha256 = hashlib.sha256()
    with open(f, 'rb') as g:
        while True:
            block = g.read(BLOCKSIZE)
            if not block:
                break
            sha256.update(block)
    return sha256.digest()

_KEYCACHE = dict()

def KDF(pwd, salt=None):
    if salt is None:
        salt = Crypto.Random.new().read(16)
    key = Crypto.Protocol.KDF.PBKDF2(pwd, salt, count=100*1000)
    return key, salt

def encrypt(f=None, s=None, key=None, salt=None, out=None, pbar=None):
    if out is None:
        out = io.BytesIO()
    if f is None:
        f = io.BytesIO(s)
    nonce = Crypto.Random.new().read(16)
    out.write(salt)
    out.write(nonce)
    out.write(NULL16BYTES)  # placeholder for tag
    cipher = Crypto.Cipher.AES.new(key, Crypto.Cipher.AES.MODE_GCM, nonce=nonce)
    while True:
        block = f.read(BLOCKSIZE)
        if not block:
            break
        out.write(cipher.encrypt(block))
        if pbar is not None:
            pbar.update(BLOCKSIZE)
    out.seek(32)
    out.write(cipher.digest())  # tag
    out.seek(0)
    return out

def decrypt(f=None, s=None, pwd=None, out=None, pbar=None):
    if out is None:
        out = io.BytesIO()
    if f is None:
        f = io.BytesIO(s)
    salt = f.read(16)
    nonce = f.read(16)
    tag = f.read(16)
    if salt not in _KEYCACHE:
        _KEYCACHE[salt] = KDF(pwd, salt)[0]
    cipher = Crypto.Cipher.AES.new(_KEYCACHE[salt], Crypto.Cipher.AES.MODE_GCM, nonce=nonce)    
    while True:
        block = f.read(BLOCKSIZE)
        if not block:
            break
        out.write(cipher.decrypt(block))
        if pbar is not None:
            pbar.update(BLOCKSIZE)
    try:
        cipher.verify(tag)
    except ValueError:
        print('Incorrect key or file corrupted.')
    out.seek(0)
    return out

def newdistantfileblock(chunkid, mtime, fsize, h, fn, key=None, salt=None):
    newdistantfile = zlib.compress(chunkid + mtime.to_bytes(8, byteorder='little', signed=False) + fsize.to_bytes(8, byteorder='little') + h + fn.encode())
    s = encrypt(s=newdistantfile, key=key, salt=salt).read()    
    return (len(s)).to_bytes(4, byteorder='little') + s

def readdistantfileblock(s, encryptionpwd):
    distantfile = zlib.decompress(decrypt(s=s, pwd=encryptionpwd).read())
    chunkid, mtime, fsize, h, fn = distantfile[:16], int.from_bytes(distantfile[16:24], byteorder='little', signed=False), int.from_bytes(distantfile[24:32], byteorder='little'), distantfile[32:64], distantfile[64:].decode()
    return chunkid, mtime, fsize, h, fn

def parseaddress(addr):
    if '@' in addr:
        user, r = addr.split('@', 1)  # split on first occurence
        if ':' in r and '/' not in user: # remote address. windows ok: impossible to have ':' after '@' in a path. linux: if a local dir is really named a@b.com:/hello/, use ./a@b.com:/hello/. what if '/' is in the username? technically possible with useradd, but not allowed by adduser, so evil corner case ignored here.
            host, path = r.split(':', 1)
            return True, user.strip(), host.strip(), path.strip()
    return False, None, None, addr       # not remote in all other cases


def threaded_upload(lock, fn, pbar, chunkid, flist,
        REQUIREDCHUNKS, DISTANTHASHES,
        mtime, fsize, h, key, salt,
        host, user, sftppwd, extra_arg, remotepath):
    """
    if file is large, then create a new thread with a new sftp connection
    to send it
    """
    with pysftp.Connection(host,
                           username=user,
                           password=sftppwd,
                           **extra_arg) as sftp:
        if sftp.isdir(remotepath):
            sftp.chdir(remotepath)
            with sftp.open(chunkid.hex() + '.tmp', 'wb') as f_enc, open(fn, 'rb') as f:
                encrypt(f, key=key, salt=salt, out=f_enc, pbar=pbar)
                sftp.rename(chunkid.hex() + '.tmp', chunkid.hex())
    with lock:
        REQUIREDCHUNKS.add(chunkid)
        DISTANTHASHES[h] = chunkid
        flist.write(newdistantfileblock(chunkid=chunkid, mtime=mtime, fsize=fsize, h=h, fn=fn, key=key, salt=salt))
    pbar.desc = str(int(pbar.desc[0])-1) + pbar.desc[1:]
    return True


def threaded_restore(f2, lock, pbar, chunkid, mtime, fn,
        host, user, sftppwd, encryptionpwd, extra_arg, path, fsize):
    """
    create a new thread to download and decrypt a file when restoring
    """
    tqdm.write(f'Restoring {fn}')
    with pysftp.Connection(host,
                           username=user,
                           password=sftppwd,
                           **extra_arg) as sftp:
        if sftp.isdir(path):
            sftp.chdir(path)
        with sftp.open(chunkid.hex(), 'rb') as g:
            with open(f2, 'wb') as f:
                decrypt(g, pwd=encryptionpwd, out=f)
    with lock:
        os.utime(f2, ns=(os.stat(f2).st_atime_ns, mtime))
    pbar.update(fsize)
    pbar.desc = str(int(pbar.desc[0])-1) + pbar.desc[1:]
    return True


def backup(src=None, dest=None, sftppwd=None, encryptionpwd=None, exclusion_list=None):
    """Do a backup of `src` (local path) to `dest` (SFTP). The files are encrypted locally and are *never* decrypted on `dest`. Also, `dest` never gets the `encryptionpwd`."""
    if os.path.isdir(src):
        os.chdir(src)
    else:
        print('Source directory does not exist.')
        return    
    if exclusion_list == None or not isinstance(exclusion_list, list):
        exclusion_list = []
    remote, user, host, remotepath = parseaddress(dest)
    if host != "localhost":
        extra_arg = {}
    else:  # necessary argument for pysftp in case of local dest backup
        cnopts = pysftp.CnOpts()
        cnopts.hostkeys = None
        extra_arg = {"cnopts":cnopts}
    if not remote or not user or not host or not remotepath:  # either not remote (local), or remote with empty user, host or remotepath
        print('dest should use the following format: user@192.168.0.2:/path/to/backup/')
        return
    print(f'Starting backup...\nSource path: {src}\nDestination host: {host}\nDestination path: {remotepath}')
    if encryptionpwd is None:
        while True:
            encryptionpwd = getpass.getpass('Please enter the encryption password: ')
            encryptionpwd_check = getpass.getpass('Confirm encryption password: ')
            if encryptionpwd != encryptionpwd_check:
                print("Passwords are not identical!\n")
            else:
                break
    key, salt = KDF(encryptionpwd)        
    for counter in range(5):
        if sftppwd is None:
            sftppwd = getpass.getpass(f'Please enter the SFTP password for user {user}: ')
        try:
            with pysftp.Connection(host, username=user, password=sftppwd, **extra_arg) as sftp:
                if sftp.isdir(remotepath):
                    sftp.chdir(remotepath)
                else:    
                    print('Destination directory does not exist.')
                    return
                ######## GET DISTANT FILES INFO
                print('Distant files list: getting...')
                DELS = b''
                DISTANTFILES = dict()
                DISTANTHASHES = dict()
                distantfilenames = set(sftp.listdir())
                DISTANTCHUNKS = {bytes.fromhex(f) for f in distantfilenames if '.' not in f}  # discard .files and .tmp files
                print("Removing old .tmp files...")
                for f in distantfilenames:
                    if f.endswith('.tmp'):
                        sftp.remove(f)
                flist = io.BytesIO()
                if sftp.isfile('.files'):
                    sftp.getfo('.files', flist)
                    flist.seek(0)
                    while True:
                        le = flist.read(4)
                        if not le:
                            break
                        length = int.from_bytes(le, byteorder='little')
                        s = flist.read(length)
                        if len(s) != length:
                            print('Item of .files is corrupt. Last sync interrupted?')
                            break                    
                        chunkid, mtime, fsize, h, fn = readdistantfileblock(s, encryptionpwd)
                        DISTANTFILES[fn] = [chunkid, mtime, fsize, h]
                        if DISTANTFILES[fn][0] == NULL16BYTES:  # deleted
                            del DISTANTFILES[fn]
                        if chunkid in DISTANTCHUNKS:
                            DISTANTHASHES[h] = chunkid      # DISTANTHASHES[sha256_noencryption] = chunkid ; even if deleted file keep the sha256, it might be useful for moved/renamed files
                else:
                    print("Remote file list not found, creating a full backup")
                for fn, distantfile in DISTANTFILES.items():
                    if not os.path.exists(fn):
                        print(f'  {fn} no longer exists (deleted or moved/renamed).')
                        DELS += newdistantfileblock(chunkid=NULL16BYTES, mtime=0, fsize=0, h=NULL32BYTES, fn=fn, key=key, salt=salt)
                if len(DELS) > 0:
                    with sftp.open('.files', 'a+') as flist:
                        flist.write(DELS)
                print('Distant files list: done.')
                ####### SEND FILES
                REQUIREDCHUNKS = set()
                with sftp.open('.files', 'a+') as flist:
                    temp_file_list = sorted(set(glob.glob('**', recursive=True)),
                                            key=get_size,
                                            reverse=larger_files_first)
                    local_file_list = []
                    for fn in temp_file_list:
                        cnt = 0
                        for item in exclusion_list:
                            if item in fn:
                                cnt += 1
                        if cnt != 0:
                            print('Exclusion rule match "' + item + '": ' + fn)
                        else:
                            local_file_list.append(fn)
                    total_size = sum([get_size(x) for x in local_file_list])
                    with tqdm(total=total_size, unit_scale=True, unit_divisor=1024, dynamic_ncols=True, smoothing=0.8, unit="B", mininterval=1, desc="0 nFreezer") as pbar:
                        threads = []
                        lock = threading.Lock()
                        for fn in local_file_list:
                            fsize = get_size(fn)
                            if os.path.isdir(fn):
                                pbar.update(fsize)
                                continue
                            try:
                                mtime = os.stat(fn).st_mtime_ns
                            except FileNotFoundError:
                                tqdm.write(f"{yel}NFS: {fn}{rst}")  # not found, skipping
                                pbar.update(fsize)
                                continue
                            if fn in DISTANTFILES and DISTANTFILES[fn][1] >= mtime and DISTANTFILES[fn][2] == fsize:
                                tqdm.write(f'US: {fn}')  # unmodified, skipping
                                pbar.update(fsize)
                                REQUIREDCHUNKS.add(DISTANTFILES[fn][0])
                            else:
                                try:
                                    h = getsha256(fn)
                                except OSError as e:
                                    tqdm.write(f"{yel}UNIX special file? Skipping: {fn}{rst}")
                                    pbar.update(fsize)
                                    continue
                                if h in DISTANTHASHES:  # ex : chunk already there with same SHA256, but other filename  (case 1 : duplicate file, case 2 : renamed/moved file)
                                    tqdm.write(f'SHS: {fn}')  # same hash, skipping
                                    chunkid = DISTANTHASHES[h]
                                    REQUIREDCHUNKS.add(chunkid) 
                                    pbar.update(fsize)
                                    flist.write(newdistantfileblock(chunkid=chunkid, mtime=mtime, fsize=fsize, h=h, fn=fn, key=key, salt=salt))
                                else:
                                    tqdm.write(f'{red}Up: {fn}{rst}')  # uploading
                                    chunkid = uuid.uuid4().bytes
                                    if fsize <= SMALL_FILE:
                                        with sftp.open(chunkid.hex() + '.tmp', 'wb') as f_enc, open(fn, 'rb') as f:
                                            encrypt(f, key=key, salt=salt, out=f_enc, pbar=None)
                                            sftp.rename(chunkid.hex() + '.tmp', chunkid.hex())
                                        REQUIREDCHUNKS.add(chunkid)
                                        DISTANTHASHES[h] = chunkid
                                        flist.write(newdistantfileblock(chunkid=chunkid, mtime=mtime, fsize=fsize, h=h, fn=fn, key=key, salt=salt))
                                        pbar.update(fsize)
                                    else:
                                        thread = threading.Thread(target=threaded_upload,
                                                                  args=(lock, fn, pbar, chunkid, flist,
                                                                      REQUIREDCHUNKS, DISTANTHASHES,
                                                                      mtime, fsize, h, key, salt,
                                                                      host, user, sftppwd, extra_arg, remotepath),
                                                                  daemon=False)
                                        pbar.desc = str(int(pbar.desc[0])+1) + pbar.desc[1:]
                                        thread.start()
                                        threads.append(thread)
                                        while sum([t.is_alive() for t in threads]) >= MAX_THREADS:
                                            time.sleep(0.5)
                        if sum([t.is_alive() for t in threads]) > 0:
                            print("Waiting for threads to finish...")
                        [t.join() for t in threads]
                print("Listing chunks to delete...")
                delchunks = DISTANTCHUNKS - REQUIREDCHUNKS
                if len(delchunks) > 0:
                    for chunkid in tqdm(delchunks,
                                   desc=f'Deleting {len(delchunks)} no-longer-used distant chunks... '
                                ):
                        sftp.remove(chunkid.hex())
            print('Backup finished.')
            break
        except paramiko.ssh_exception.AuthenticationException:
            print(red + 'Authentication failed.' + rst)
            continue
        except paramiko.ssh_exception.SSHException as e:
            print(e, '\nPlease ssh your remote host at least once before, or add your remote to your known_hosts file.\n\n')  # todo: avoid ugly error messages after
            continue
        break

def restore(src=None, dest=None,
        sftppwd=None, encryptionpwd=None,
        print_file_list=False,
        only_print_file_list=False,
        include_regex=".*",
        exclude_regex='^/'):
    """Restore encrypted files from `src` (SFTP or local path) to `dest` (local path)."""
    if encryptionpwd is None:
        while True:
            encryptionpwd = getpass.getpass('Please enter the decryption password: ')
            encryptionpwd_check = getpass.getpass('Confirm decryption password: ')
            if encryptionpwd != encryptionpwd_check:
                print("Passwords are not identical!\n")
            else:
                break
    remote, user, host, path = parseaddress(src)
    if host != "localhost":
        extra_arg = {}
    else:  # necessary argument for pysftp in case of local dest backup
        cnopts = pysftp.CnOpts()
        cnopts.hostkeys = None
        extra_arg = {"cnopts":cnopts}
    if remote:
        if sftppwd is None:
            sftppwd = getpass.getpass(f'Please enter the SFTP password for user {user}: ')
        if not user or not host or not path:
            print('src should be either a local directory, or a remote using the following format: user@192.168.0.2:/path/to/backup/')
            return
        src_cm = pysftp.Connection(host, username=user, password=sftppwd, **extra_arg)

    else:
        src_cm = nullcontext()
        src_cm.open, src_cm.chdir, src_cm.isdir = open, os.chdir, os.path.isdir
    with src_cm:
        DISTANTFILES = dict()
        dest = os.path.abspath(dest)
        if src_cm.isdir(path):
            src_cm.chdir(path)
        else:    
            print('src path does not exist.')
            return
        print('Restoring backup from %s: %s\nDestination local path: %s' % ('remote' if remote else 'local path', src, dest))

        with src_cm.open('.files', 'rb') as flist:
            print("Fetching remote file list...")
            flist = io.BytesIO(flist.read())

            buf = []
            while True:
                l = flist.read(4)
                if not l:
                    break
                length = int.from_bytes(l, byteorder='little')
                s = flist.read(length)

                if len(s) != length:
                    print(red + 'An item of the remote file list (.files) is corrupt, ignored. Last sync interrupted?' + rst)
                    break
                buf.append(s)

            def threaded_flist_decrypt(lock, pbar, s, encryptionpwd, DISTANTFILES):
                """
                decrypts flist content using multithreading
                """
                chunkid, mtime, fsize, h, fn = readdistantfileblock(s, encryptionpwd)
                with lock:
                    DISTANTFILES[fn] = [chunkid, mtime, fsize, h]
                    if DISTANTFILES[fn][0] == NULL16BYTES:  # deleted
                        del DISTANTFILES[fn]
                pbar.update(len(s))

            with tqdm(total=sum([len(s) for s in buf]),
                      unit="B",
                      dynamic_ncols=True,
                      unit_scale=True,
                      unit_divisor=1024,
                      smoothing=0.1,
                      desc="Decrypting file list") as pbar:
                threads = []
                lock = threading.Lock()
                for ss in buf:
                    if len(ss) <= SMALL_FILE:
                        threaded_flist_decrypt(lock, pbar, ss, encryptionpwd,
                                DISTANTFILES)
                    else:
                        thread = threading.Thread(target=threaded_flist_decrypt,
                                                  args=(lock, pbar, ss,
                                                        encryptionpwd,
                                                        DISTANTFILES))
                        thread.start()
                        threads.append(thread)
                        while sum([t.is_alive() for t in threads]) >= MAX_THREADS:
                            time.sleep(0.5)
                [t.join() for t in threads]
            if only_print_file_list is True:
                if only_print_file_list is True:
                    with open("distant_file_list.txt", "a") as f:
                        f.write(str([x["fn"] for x in DISTANTFILES]))
                print("Written to file distant_file_list.txt")
                raise SystemExit()

        pbar = tqdm(total=sum(x[2] for x in DISTANTFILES.values()),
                    smoothing=0.1,
                    dynamic_ncols=True,
                    desc="0 Restoring files",
                    unit_scale=True,
                    unit_divisor=1024,
                    unit="B")
        lock = threading.Lock()
        threads = []

        dist_list = sorted(list( DISTANTFILES.items()),
                            key = lambda x : x[1][2],
                            reverse = larger_files_first)
        for fn, [chunkid, mtime, fsize, h] in dist_list:
            if re.match(include_regex, fn) is None:
                tqdm.write(f"Inclusion regex mismatch: {fn}")
                break
            if re.match(exclude_regex, fn) is not None:
                tqdm.write(f"Exclusion regex match: {fn}")
                break
            f2 = os.path.join(dest, fn).replace('\\', '/')
            os.makedirs(os.path.dirname(f2), exist_ok=True)
            if os.path.exists(f2) and getsha256(f2) == h:
                tqdm.write(f'{yel}APS: {fn}{rst}')  # already present, skipping
                continue
            if fsize <= SMALL_FILE:
                tqdm.write(f'{red}R: {fn}{rst}')  # restoring
                with open(f2, 'wb') as f, src_cm.open(chunkid.hex(), 'rb') as g:
                    decrypt(g, pwd=encryptionpwd, out=f)
                os.utime(f2, ns=(os.stat(f2).st_atime_ns, mtime))
                pbar.update(fsize)
            else:
                thread = threading.Thread(target=threaded_restore,
                        args=(f2, lock, pbar, chunkid, mtime, fn,
                            host, user, sftppwd, encryptionpwd, extra_arg,
                            path, fsize))
                thread.start()
                pbar.desc = str(int(pbar.desc[0])+1) + pbar.desc[1:]
                threads.append(thread)
                while sum([t.is_alive() for t in threads]) >= MAX_THREADS:
                    time.sleep(0.5)
        pbar.close()
        print('Restore finished.')


def console_script():
    """Command-line script"""
    if len(sys.argv) >= 4:
        if sys.argv[1] == 'backup':
            try:
                excl = sys.argv[4]
            except:
                excl = []
            backup(src=sys.argv[2], dest=sys.argv[3], exclusion_list=excl)
        elif sys.argv[1] == 'restore':
            restore(src=sys.argv[2], dest=sys.argv[3])
    else:
        print('Missing arguments.\nExamples:\n  nfreezer backup test/ user@192.168.0.2:/test/ \'["mkv", "avi"]\'\n  nfreezer restore user@192.168.0.2:/test/ restored/')

