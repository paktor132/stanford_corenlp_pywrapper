"""
the socket server approach.
this is eventually the right way to do it, but not tested well yet
"""

from __future__ import division
import subprocess, tempfile, time, os, logging, re, struct, socket, atexit
try:
    import ujson as json
except ImportError:
    import json

logging.basicConfig()  # wtf, why we have to call this?
LOG = logging.getLogger("StanfordSocketWrap")
LOG.setLevel("INFO")
# LOG.setLevel("DEBUG")

COMMAND = """
exec {JAVA} -Xmx{XMX_AMOUNT} -cp {classpath}
    corenlp.PipeCommandRunner --server {server_port} {more_config}"""

JAVA = "java"
XMX_AMOUNT = "4g"

PARSEDOC_TIMEOUT_SEC = 60 * 5
STARTUP_BUSY_WAIT_INTERVAL_SEC = 2.0

#arg for mkstemp(dir=), so if None it defaults to somewhere
TEMP_DIR = None


def command(mode=None, configfile=None, **kwargs):
    d = {}
    d.update(globals())
    d.update(**kwargs)

    more_config = ""
    if mode:
        more_config += " --mode {}".format(mode)
    if configfile:
        more_config += " --configfile {}".format(configfile)
    d['more_config'] = more_config


    return COMMAND.format(**d).replace("\n", " ")


class SubprocessCrashed(Exception):
    pass


class SockWrap:

    def __init__(self, mode=None, server_port=12340, configfile=None,
            corenlp_libdir=os.path.join(os.environ['HOME'], "stanford-corenlp"),
            corenlp_jars=("stanford-corenlp-3.4.jar","stanford-corenlp-3.4-models.jar","stanford-srparser-2014-07-01-models.jar")
            ):
        self.mode = mode
        self.proc = None
        self.server_port = server_port
        self.configfile = configfile
        self.corenlp_libdir = corenlp_libdir

        corenlp_jar_fullfilenames = [os.path.join(corenlp_libdir, f) for f in corenlp_jars]
        assert any(os.path.exists(f) for f in corenlp_jar_fullfilenames), "CoreNLP jar file does not seem to exist; are the paths correct?  Searched files: %s" % repr(corenlp_jar_fullfilenames)

        local_libdir = os.path.join(os.path.abspath(os.path.dirname(__file__)),
                                    'lib')

        jars = [os.path.join(local_libdir, "piperunner.jar"),
                # for eclipse development only
                # "/Users/brendano/myutil/bin",
                os.path.join(local_libdir, "guava-13.0.1.jar"),
                os.path.join(local_libdir, "jackson-all-1.9.11.jar"),
        ]

        jars += corenlp_jar_fullfilenames
        self.classpath = ':'.join(jars)

        # LOG.info("CLASSPATH: " + self.classpath)

        self.start_server()
        # This probably is only half-reliable, but worth a shot.
        atexit.register(self.kill_proc_if_running)

    def __del__(self):
        # This is also an unreliable way to ensure the subproc is gone, but
        # might as well try
        self.kill_proc_if_running()

    def start_server(self):
        self.kill_proc_if_running()
        cmd = command(mode=self.mode, server_port=self.server_port,
                      configfile=self.configfile,
                      classpath=self.classpath)
        LOG.info("Starting pipe subprocess, and waiting for signal it's ready, with command: %s" % cmd)
        self.proc = subprocess.Popen(cmd, shell=True)
        while True:
            time.sleep(STARTUP_BUSY_WAIT_INTERVAL_SEC)
            try:
                ret = self.send_command_and_parse_result('PING\t""', 2)
                if ret is None:
                    continue
                assert ret == "PONG", "Bad return data on startup ping: " + ret
                LOG.info("Successful ping. The server has started.")
                break
            except socket.error, e:
                LOG.info("Waiting for startup: ping got exception: %s %s" % (type(e), e))

        LOG.info("Subprocess is ready.")

    def ensure_proc_is_running(self):
        if self.proc is None:
            # Has never been started
            self.start_server()
        elif self.proc.poll() is not None:
            # Restart
            self.start_server()

    def kill_proc_if_running(self):
        if self.proc is None:
            # it's never been started yet
            return
        retcode = self.proc.poll()
        if retcode is not None:
            LOG.info("Subprocess seems to be stopped, exit code %s" % retcode)
        elif retcode is None:
            LOG.warning("Killing subprocess %s" % self.proc.pid)
            os.kill(self.proc.pid, 9)

    def parse_doc(self, text, timeout=PARSEDOC_TIMEOUT_SEC):
        cmd = "PARSEDOC\t%s" % json.dumps(text)
        return self.send_command_and_parse_result(cmd, timeout)

    def get_socket(self, num_retries=3, retry_interval=1):
        # could be smarter here about reusing the same socket?
        for trial in range(num_retries):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                # sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) # not sure if this is needed?
                sock.connect(('localhost', self.server_port))
                return sock
            except (socket.error, socket.timeout) as e:
                LOG.info("socket error when making connection (%s)" % e)
                if trial < num_retries-1:
                    LOG.info("pausing before retry")
                    time.sleep(retry_interval)
        assert False, "couldnt connect socket"

    def send_command_and_parse_result(self, cmd, timeout):
        try:
            self.ensure_proc_is_running()
            data = self.send_command_and_get_string_result(cmd, timeout)
            decoded = None
            try:
                decoded = json.loads(data)
            except ValueError:
                LOG.warning("Bad JSON returned from subprocess; returning null.")
                return None
            return decoded
        except socket.timeout, e:
            LOG.info("Socket timeout happened, returning None: %s %s" % (type(e), e))
            return None
            # This is tricky. maybe the process is running smoothly but just
            # taking longer than we like.  if it's in thie state, and we try to
            # send another command, what happens?  Should we forcibly restart
            # the process now just in case?

    def send_command_and_get_string_result(self, cmd, timeout):
        sock = self.get_socket()
        sock.settimeout(timeout)
        sock.sendall(cmd + "\n")
        size_info_str = sock.recv(8)
        # java "long" is 8 bytes, which python struct calls "long long".
        # java default byte ordering is big-endian.
        size_info = struct.unpack('>Q', size_info_str)[0]
        data = sock.recv(size_info)
        return data


def test_simple():
    assert_no_java("no java when starting")

    p = SockWrap("ssplit")
    ret = p.parse_doc("Hello world.")
    print ret
    assert len(ret['sentences']) == 1
    assert u' '.join(ret['sentences'][0]['tokens']) == u"Hello world ."

    p.kill_proc_if_running()
    assert_no_java()

def test_paths():
    import pytest
    with pytest.raises(AssertionError):
        SockWrap("ssplit", corenlp_libdir="blabla_bad_dir")

def assert_no_java(msg=""):
    ps_output = os.popen("ps wux").readlines()
    javalines = [x for x in ps_output if re.search(r'\bbin/java\b', x)]
    print ''.join(javalines)
    assert len(javalines) == 0, msg

# def test_doctimeout():
#     assert_no_java("no java when starting")
#
#     p = SockWrap("pos")
#     ret = p.parse_doc(open("allbrown.txt").read(), 0.5)
#     assert ret is None
#     p.kill_proc_if_running()
#     assert_no_java()
#
# def test_crash():
#     assert_no_java("no java when starting")
#     p = SockWrap("ssplit")
#     p.crash()
#     ret = p.parse_doc("Hello world.")
#     assert len(ret['sentences'])==1
#
#     p.kill_proc_if_running()
#     assert_no_java()
