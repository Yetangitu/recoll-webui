#!/usr/bin/env python
#{{{ debug
# debug
from __future__ import print_function
import sys

def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)
#}}}
#{{{ imports
import os
import bottle
import time
import sys
import datetime
import glob
import hashlib
import csv
import StringIO
import ConfigParser
import string
import shlex
import urllib

# use ujson if avalible (faster than built in json)
try:
    import ujson as json
except ImportError:
    import json
    print("ujson module not found, using (slower) built-in json module instead")

# import recoll and rclextract
try:
    from recoll import recoll
    from recoll import rclextract
    hasrclextract = True
except:
    import recoll
    hasrclextract = False
# import rclconfig system-wide or local copy
try:
    from recoll import rclconfig
except:
    import rclconfig
#}}}
#{{{ settings
# settings defaults
DEFAULTS = {
    'context': 30,
    'stem': 1,
    'timefmt': '%c',
    'dirdepth': 2,
    'maxchars': 500,
    'maxresults': 0,
    'perpage': 25,
    'csvfields': 'filename title author size time mtype url',
    'title_link': 'download',
}

# sort fields/labels
SORTS = [
    ("relevancyrating", "Relevancy"),
    ("mtime", "Date",),
    ("url", "Path"),
    ("filename", "Filename"),
    ("fbytes", "Size"),
    ("author", "Author"),
]

# doc fields
FIELDS = [
    # exposed by python api
    'ipath',
    'filename',
    'title',
    'author',
    'fbytes',
    'dbytes',
    'size',
    'fmtime',
    'dmtime',
    'mtime',
    'mtype',
    'origcharset',
    'sig',
    'relevancyrating',
    'url',
    'abstract',
    'keywords',
    # calculated
    'time',
    'snippet',
    'label',
]
#}}}
#{{{  functions
#{{{  helpers
def select(ls, invalid=[None]):
    for value in ls:
        if value not in invalid:
            return value

def timestr(secs, fmt):
    if secs == '' or secs is None:
        secs = '0'
    t = time.gmtime(int(secs))
    return time.strftime(fmt, t)

def normalise_filename(fn):
    valid_chars = "_-%s%s" % (string.ascii_letters, string.digits)
    out = ""
    for i in range(0,len(fn)):
        if fn[i] in valid_chars:
            out += fn[i]
        else:
            out += "_"
    return out

def get_topdirs(db):
    rclconf = rclconfig.RclConfig(os.path.dirname(db))
    return rclconf.getConfParam('topdirs')
#}}}
#{{{ get_config
def get_config():
    config = {}
    # get useful things from recoll.conf
    rclconf = rclconfig.RclConfig()
    config['confdir'] = rclconf.getConfDir()
    config['extradbs'] = []
    if 'RECOLL_EXTRA_DBS' in os.environ:
        config['extradbs'] = os.environ.get('RECOLL_EXTRA_DBS').split(':')
    config['dirs']={}
    for dir in [os.path.expanduser(d) for d in
                   shlex.split(rclconf.getConfParam('topdirs'))]:
        config['dirs'][dir] = os.path.join(config['confdir'], 'xapiandb')
    # global options as set by the default recoll config are also used for extra databases
    # when searching the entire set
    config['stemlang'] = rclconf.getConfParam('indexstemminglanguages')
    # get config from cookies or defaults
    for k, v in DEFAULTS.items():
        value = select([bottle.request.get_cookie(k), v])
        config[k] = type(v)(value)
    # Fix csvfields: get rid of invalid ones to avoid needing tests in the dump function
    cf = config['csvfields'].split()
    ncf = [f for f in cf if f in FIELDS]
    config['csvfields'] = ' '.join(ncf)
    config['fields'] = ' '.join(FIELDS)
    # get additional databases
    for e in config['extradbs']:
        for t in [os.path.expanduser(d) for  d in
                    shlex.split(get_topdirs(e))]:
            config['dirs'][t] = e
    # get mountpoints
    config['mounts'] = {}
    for d,db in config['dirs'].items():
        name = 'mount_%s' % urllib.quote(d,'')
        config['mounts'][d] = select([bottle.request.get_cookie(name), 'file://%s' % d], [None, ''])
    return config
#}}}
#{{{ get_dirs
def get_dirs(dirs, depth):
    v = []
    for dir,d in dirs.items():
        dirs = [dir]
        for d in range(1, depth+1):
            dirs = dirs + glob.glob(dir + '/*' * d)
        dirs = filter(lambda f: os.path.isdir(f), dirs)
        top_path = dir.rsplit('/', 1)[0]
        dirs = [w.replace(top_path+'/', '', 1) for w in dirs]
        v = v + dirs
    return ['<all>'] + v
#}}}
#{{{ get_query
def get_query():
    query = {
        'query': select([bottle.request.query.get('query'), '']),
        'before': select([bottle.request.query.get('before'), '']),
        'after': select([bottle.request.query.get('after'), '']),
        'dir': select([bottle.request.query.get('dir'), '', '<all>'], [None, '']),
        'sort': select([bottle.request.query.get('sort'), SORTS[0][0]]),
        'ascending': int(select([bottle.request.query.get('ascending'), 0])),
        'page': int(select([bottle.request.query.get('page'), 0])),
        'highlight': int(select([bottle.request.query.get('highlight'), 1])),
        'snippets': int(select([bottle.request.query.get('snippets'), 1])),
    }
    return query
#}}}
#{{{ query_to_recoll_string
def query_to_recoll_string(q):
    qs = q['query'].decode('utf-8')
    if len(q['after']) > 0 or len(q['before']) > 0:
        qs += " date:%s/%s" % (q['after'], q['before'])
    if q['dir'] != '<all>':
        qs += " dir:\"%s\" " % q['dir'].decode('utf-8')
    return qs
#}}}
#{{{ recoll_initsearch
def recoll_initsearch(q):
    config = get_config()
    """ The reason for this somewhat elaborate scheme is to keep the
    set size as small as possible by searching only those databases
    with matching topdirs """
    if q['dir'] == '<all>':
        db = recoll.connect(config['confdir'], config['extradbs'])
    else:
        dbs=[]
        for d,db in config['dirs'].items():
            if os.path.commonprefix([os.path.basename(d),q['dir']]) == q['dir']:
                dbs.append(db)
        if len(dbs) == 0: 
            # should not happen, using non-existing q['dir']?
            db = recoll.connect(config['confdir'],config['extradbs'])
        elif len(dbs) == 1:
            # only one db (most common situation)
            db = recoll.connect(os.path.dirname(dbs[0]))
        else:
            # more than one db with matching topdir, use 'm all
            db = recoll.connect(dbs[0],dbs[1:])
    db.setAbstractParams(config['maxchars'], config['context'])
    query = db.query()
    query.sortby(q['sort'], q['ascending'])
    try:
        qs = query_to_recoll_string(q)
        query.execute(qs, config['stem'], config['stemlang'])
    except:
        pass
    return query
#}}}
#{{{ HlMeths
class HlMeths:
    def startMatch(self, idx):
        return '<span class="search-result-highlight">'
    def endMatch(self):
        return '</span>'
#}}}
#{{{ recoll_search
def recoll_search(q):
    config = get_config()
    tstart = datetime.datetime.now()
    highlighter = HlMeths()
    results = []
    query = recoll_initsearch(q)
    nres = query.rowcount

    if config['maxresults'] == 0:
        config['maxresults'] = nres
    if nres > config['maxresults']:
        nres = config['maxresults']
    if config['perpage'] == 0 or q['page'] == 0:
        config['perpage'] = nres
        q['page'] = 1
    offset = (q['page'] - 1) * config['perpage']

    if query.rowcount > 0 and offset < query.rowcount:
        if type(query.next) == int:
            query.next = offset
        else:
            query.scroll(offset, mode='absolute')

        for i in range(config['perpage']):
            try:
                doc = query.fetchone()
            except:
                break
            d = {}
            for f in FIELDS:
                v = getattr(doc, f)
                if v is not None:
                    d[f] = v.encode('utf-8')
                else:
                    d[f] = ''
            d['label'] = select([d['title'], d['filename'], '?'], [None, ''])
            d['sha'] = hashlib.sha1(d['url']+d['ipath']).hexdigest()
            d['time'] = timestr(d['mtime'], config['timefmt'])
            if q['snippets']:
                if q['highlight']:
                    d['snippet'] = query.makedocabstract(doc, highlighter).encode('utf-8')
                else:
                    d['snippet'] = query.makedocabstract(doc).encode('utf-8')
            results.append(d)
    tend = datetime.datetime.now()
    return results, nres, tend - tstart
#}}}
#}}}
#{{{ routes
#{{{ static
@bottle.route('/static/:path#.+#')
def server_static(path):
    return bottle.static_file(path, root='./static')
#}}}
#{{{ main
@bottle.route('/')
@bottle.view('main')
def main():
    config = get_config()
    return { 'dirs': get_dirs(config['dirs'], config['dirdepth']),
            'query': get_query(), 'sorts': SORTS }
#}}}
#{{{ results
@bottle.route('/results')
@bottle.view('results')
def results():
    config = get_config()
    query = get_query()
    qs = query_to_recoll_string(query)
    res, nres, timer = recoll_search(query)
    if config['maxresults'] == 0:
        config['maxresults'] = nres
    if config['perpage'] == 0:
        config['perpage'] = nres
    return { 'res': res, 'time': timer, 'query': query, 'dirs':
            get_dirs(config['dirs'], config['dirdepth']),
             'qs': qs, 'sorts': SORTS, 'config': config,
            'query_string': bottle.request.query_string, 'nres': nres,
             'hasrclextract': hasrclextract }
#}}}
#{{{ preview
@bottle.route('/preview/<resnum:int>')
def preview(resnum):
    if not hasrclextract:
        return 'Sorry, needs recoll version 1.19 or later'
    query = get_query()
    qs = query_to_recoll_string(query)
    rclq = recoll_initsearch(query)
    if resnum > rclq.rowcount - 1:
        return 'Bad result index %d' % resnum
    rclq.scroll(resnum)
    doc = rclq.fetchone()
    xt = rclextract.Extractor(doc)
    tdoc = xt.textextract(doc.ipath)
    if tdoc.mimetype == 'text/html':
        bottle.response.content_type = 'text/html; charset=utf-8'
    else:
        bottle.response.content_type = 'text/plain; charset=utf-8'
    return tdoc.text
#}}}
#{{{ download
@bottle.route('/download/<resnum:int>')
def edit(resnum):
    if not hasrclextract:
        return 'Sorry, needs recoll version 1.19 or later'
    query = get_query()
    qs = query_to_recoll_string(query)
    rclq = recoll_initsearch(query)
    if resnum > rclq.rowcount - 1:
        return 'Bad result index %d' % resnum
    rclq.scroll(resnum)
    doc = rclq.fetchone()
    bottle.response.content_type = doc.mimetype
    pathismine = False
    if doc.ipath == '':
        # If ipath is null, we can just return the file
        path = doc.url.replace('file://','')
    else:
        # Else this is a subdocument, extract to temporary file
        xt = rclextract.Extractor(doc)
        path = xt.idoctofile(doc.ipath, doc.mimetype)
        pathismine = True
    bottle.response.headers['Content-Disposition'] = \
        'attachment; filename="%s"' % os.path.basename(path).encode('utf-8')
    path = path.encode('utf-8')
    bottle.response.headers['Content-Length'] = os.stat(path).st_size
    f = open(path, 'r')
    if pathismine:
        os.unlink(path)
    return f
#}}}
#{{{ json
@bottle.route('/json')
def get_json():
    query = get_query()
    qs = query_to_recoll_string(query)
    bottle.response.headers['Content-Type'] = 'application/json'
    bottle.response.headers['Content-Disposition'] = 'attachment; filename=recoll-%s.json' % normalise_filename(qs)
    res, nres, timer = recoll_search(query)

    return json.dumps({ 'query': query, 'nres': nres, 'results': res })
#}}}
#{{{ csv
@bottle.route('/csv')
def get_csv():
    config = get_config()
    query = get_query()
    query['page'] = 0
    query['snippets'] = 0
    qs = query_to_recoll_string(query)
    bottle.response.headers['Content-Type'] = 'text/csv'
    bottle.response.headers['Content-Disposition'] = 'attachment; filename=recoll-%s.csv' % normalise_filename(qs)
    res, nres, timer = recoll_search(query)
    si = StringIO.StringIO()
    cw = csv.writer(si)
    fields = config['csvfields'].split()
    cw.writerow(fields)
    for doc in res:
        row = []
        for f in fields:
            row.append(doc[f])
        cw.writerow(row)
    return si.getvalue().strip("\r\n")
#}}}
#{{{ settings/set
@bottle.route('/settings')
@bottle.view('settings')
def settings():
    return get_config()

@bottle.route('/set')
def set():
    config = get_config()
    for k, v in DEFAULTS.items():
        bottle.response.set_cookie(k, str(bottle.request.query.get(k)), max_age=3153600000, expires=315360000)
    for d,db in config['dirs'].items():
        cookie_name = 'mount_%s' % urllib.quote(d, '')
        bottle.response.set_cookie(cookie_name, str(bottle.request.query.get('mount_%s' % d)), max_age=3153600000, expires=315360000)
    bottle.redirect('./')
#}}}
#{{{ osd
@bottle.route('/osd.xml')
@bottle.view('osd')
def main():
    #config = get_config()
    url = bottle.request.urlparts
    url = '%s://%s' % (url.scheme, url.netloc)
    return {'url': url}
#}}}
# vim: fdm=marker:tw=80:ts=4:sw=4:sts=4:et

