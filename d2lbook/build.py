import os
import sys
import notedown
import nbformat
import nbconvert
import pkg_resources
import logging
import shutil
import time
import argparse
import re
import hashlib
from d2lbook.utils import *
from d2lbook.sphinx import prepare_sphinx_env
from d2lbook.config import Config

__all__  = ['build']

# our special mark in markdown, e.g. :label:`chapter_intro`
mark_re_md = re.compile(':([-\/\\._\w\d]+):`([\*-\/\\\._\w\d]+)`')
# the according one in rst, changed ` to ``
mark_re = re.compile(':([-\/\\._\w\d]+):``([\*-\/\\\._\w\d]+)``')

commands = ['eval', 'rst', 'html', 'pdf', 'pkg', 'linkcheck',
            'outputcheck', 'lib', 'all']

def build():
    parser = argparse.ArgumentParser(description='Build the documents')
    parser.add_argument('commands', nargs='+', choices=commands)
    args = parser.parse_args(sys.argv[2:])
    config = Config()
    builder = Builder(config)
    for cmd in args.commands:
        getattr(builder, cmd)()

class Builder(object):
    def __init__(self, config):
        self.config = config
        mkdir(self.config.tgt_dir)
        self.sphinx_opts = '-j 4'
        if config.build['warning_is_error'].lower() == 'true':
            self.sphinx_opts += ' -W'
        self.done = dict((cmd, False) for cmd in commands)

    def _find_md_files(self):
        build = self.config.build
        src_dir = self.config.src_dir
        excluded_files = find_files(build['exclusions'], src_dir)
        pure_markdowns = find_files(build['non-notebooks'], src_dir)
        pure_markdowns = [fn for fn in pure_markdowns if fn not in excluded_files]
        notebooks = find_files(build['notebooks'], src_dir)
        notebooks = [fn for fn in notebooks if fn not in pure_markdowns and fn
                     not in pure_markdowns and fn not in excluded_files]
        depends = find_files(build['dependencies'], src_dir)
        return sorted(notebooks), sorted(pure_markdowns), sorted(depends)

    def _get_updated_md_files(self):
        notebooks, pure_markdowns, depends = self._find_md_files()
        depends_mtimes = get_mtimes(depends)
        latest_depend = max(depends_mtimes) if len(depends_mtimes) else 0
        updated_notebooks = get_updated_files(
            notebooks, self.config.src_dir, self.config.eval_dir, 'md', 'ipynb', latest_depend)
        updated_markdowns = get_updated_files(
            pure_markdowns, self.config.src_dir, self.config.eval_dir, 'md', 'md', latest_depend)
        return updated_notebooks, updated_markdowns

    def outputcheck(self):
        notebooks, _, _ = self._find_md_files()
        reader = notedown.MarkdownReader()
        error = False
        for fn in notebooks:
            with open(fn, 'r') as f:
                notebook= reader.read(f)
            for c in notebook.cells:
                if 'outputs' in c and len(c['outputs']):
                    logging.error("Found execution outputs in %s", fn)
                    error = True
        if error:
            exit(-1)

    def eval(self):
        """Evaluate the notebooks and save them in a different folder"""
        if self.done['eval']:
            return
        self.done['eval'] = True
        notebooks, pure_markdowns, depends = self._find_md_files()
        depends_mtimes = get_mtimes(depends)
        latest_depend = max(depends_mtimes) if len(depends_mtimes) else 0
        updated_notebooks = get_updated_files(
            notebooks, self.config.src_dir, self.config.eval_dir, 'md', 'ipynb', latest_depend)
        updated_markdowns = get_updated_files(
            pure_markdowns, self.config.src_dir, self.config.eval_dir, 'md', 'md', latest_depend)

        logging.info('%d notedowns and %d markdowns are out dated',
                     len(updated_notebooks), len(updated_markdowns))
        self._copy_resources(self.config.src_dir, self.config.eval_dir)
        for src, tgt in updated_notebooks:
            logging.info('Evaluating %s, save as %s', src, tgt)
            mkdir(os.path.dirname(tgt))
            start = time.time()
            run_cells = self.config.build['eval_notebook'].lower()
            eval_notebook(src, tgt, run_cells=='true')
            logging.info('Finished in %.1f sec', time.time() - start)

        for src, tgt in updated_markdowns:
            logging.info('Copy %s to %s', src, tgt)
            mkdir(os.path.dirname(tgt))
            shutil.copyfile(src, tgt)

        src_pattern = self.config.build['non-notebooks'] + ' ' + self.config.build['notebooks']
        removed_notebooks = get_removed_files(src_pattern, self.config.src_dir,
                                              self.config.eval_dir, 'md', 'ipynb')
        if removed_notebooks:
            logging.info('Clean removed notebooks %s', ','.join(removed_notebooks))
            for fn in removed_notebooks:
                os.remove(fn)

    def _copy_resources(self, src_dir, tgt_dir):
        resources = self.config.build['resources']
        if resources:
            logging.info('Copy resources "%s" from %s to %s',
                         resources, src_dir, tgt_dir)
        for res in resources.split():
            src = os.path.join(src_dir, res)
            updated = get_updated_files(find_files(src), src_dir, tgt_dir)
            for src, tgt in updated:
                if os.path.isdir(src):
                    continue
                mkdir(os.path.dirname(tgt))
                shutil.copyfile(src, tgt)

    def _copy_rst(self):
        rst_files = find_files(self.config.build['rsts'], self.config.src_dir)
        rst_files = [fn for fn in rst_files if self.config.rst_dir not in fn]
        updated_rst = get_updated_files(rst_files, self.config.src_dir, self.config.rst_dir)
        if len(updated_rst):
            logging.info('Copy %d updated RST files to %s',
                         len(updated_rst), self.config.rst_dir)
        for src, tgt in updated_rst:
            copy(src, tgt)

    def rst(self):
        if self.done['rst']:
            return
        self.done['rst'] = True
        self.eval()
        notebooks = find_files(os.path.join(self.config.eval_dir, '**', '*.ipynb'))
        updated_notebooks = get_updated_files(
            notebooks, self.config.eval_dir, self.config.rst_dir, 'ipynb', 'rst')
        logging.info('%d rst files are outdated', len(updated_notebooks))
        for src, tgt in updated_notebooks:
            logging.info('Convert %s to %s', src, tgt)
            mkdir(os.path.dirname(tgt))
            ipynb2rst(src, tgt)
        prepare_sphinx_env(self.config)
        self._copy_rst()
        self._copy_resources(self.config.src_dir, self.config.rst_dir)

    def html(self):
        if self.done['html']:
            return
        self.done['html'] = True
        self.rst()
        run_cmd(['sphinx-build', self.config.rst_dir, self.config.html_dir,
                 '-b html -c', self.config.rst_dir, self.sphinx_opts])

    def linkcheck(self):
        if self.done['linkcheck']:
            return
        self.done['linkcheck'] = True
        self.rst()
        run_cmd(['sphinx-build', self.config.rst_dir, self.config.linkcheck_dir,
                 '-b linkcheck -c', self.config.rst_dir, self.sphinx_opts])

    def pdf(self):
        if self.done['pdf']:
            return
        self.done['pdf'] = True
        self.rst()
        run_cmd(['sphinx-build ', self.config.rst_dir, self.config.pdf_dir,
                 '-b latex -c', self.config.rst_dir, self.sphinx_opts])

        run_cmd(['cd', self.config.pdf_dir, '&& make'])

    def pkg(self):
        if self.done['pkg']:
            return
        self.done['pkg'] = True
        self.eval()
        zip_fname = 'out.zip'
        run_cmd(['cd', self.config.eval_dir, '&& zip -r',
                 zip_fname, '*'])
        shutil.move(os.path.join(self.config.eval_dir, zip_fname),
                    self.config.pkg_fname)

    def lib(self):
        save_mark = self.config.library['save_mark']
        if not save_mark:
            logging.info('No save mark is specified, ignoring...')
            return
        lib_fname = self.config.library['save_filename']
        logging.info('Matching with the partten: "%s"', save_mark)

        root = os.path.join(self.config.src_dir, self.config.build['index'] + '.md')
        notebooks = get_toc(root)
        notebooks_enabled, _, _ = self._find_md_files()
        notebooks = [nb for nb in notebooks if nb in notebooks_enabled]
        with open(lib_fname, 'w') as f:
            lib_name = os.path.dirname(lib_fname)
            assert not '/' in lib_name, lib_name
            f.write('# This file is generated automatically through:\n')
            f.write('#    d2lbook build lib\n')
            f.write('# Don\'t edit it directly\n\n')
            f.write('import sys\n'+lib_name+' = sys.modules[__name__]\n\n')

            for nb in notebooks:
                blocks = get_codes_to_save(nb, save_mark)
                if blocks:
                    logging.info('Found %d blocks in %s', len(blocks), nb)
                    for block in blocks:
                        logging.info(' --- %s', block[0])
                        codes = '# Defined in file: %s\n%s\n\n' %(
                            nb, '\n'.join(block))
                        f.write(codes)

        logging.info('Saved into %s', lib_fname)

    def all(self):
        self.eval()
        self.rst()
        self.html()
        self.pdf()
        self.pkg()

def get_codes_to_save(input_fn, save_mark):
    """get the codes blocks (import, class, def) and will be saved"""
    reader = notedown.MarkdownReader(match='strict')
    with open(input_fn, 'r') as f:
        nb = reader.read(f)
    saved = []
    for cell in nb.cells:
        if cell.cell_type == 'code':
            lines = cell.source.split('\n')
            for i, l in enumerate(lines):
                if l.strip().startswith('#') and save_mark in l:
                    block = [lines[i+1]]
                    if lines[i+1].startswith('import') or lines[i+1].startswith('from'):
                        for j in range(i+2, len(lines)):
                            l = lines[j]
                            if l.startswith(' ') or not l:
                                break
                            block.append(l)
                    else:
                        for j in range(i+2, len(lines)):
                            l = lines[j]
                            if not l.startswith(' ') and len(l):
                                break
                            block.append(l)
                    saved.append(block)
    return saved


def get_toc(root):
    """return a list of files in the order defined by TOC"""
    subpages = get_subpages(root)
    res = [root]
    for fn in subpages:
        res.extend(get_toc(fn))
    return res

def get_subpages(input_fn):
    """read toc in input_fn, returns what it contains"""
    subpages = []
    reader = notedown.MarkdownReader()
    with open(input_fn, 'r') as f:
        nb = reader.read(f)
    for cell in nb.cells:
        if (cell.cell_type == 'code' and
            'attributes' in cell.metadata and
            'toc' in cell.metadata.attributes['classes']):
            for l in cell.source.split('\n'):
                l = l.strip()
                if not l.startswith(':'):
                    fn = os.path.join(os.path.dirname(input_fn), l+'.md')
                    if os.path.exists(fn):
                        subpages.append(fn)
    return subpages

def eval_notebook(input_fn, output_fn, run_cells, timeout=20*60, lang='python'):
    # process: add empty lines before and after a mark, otherwise it confuses
    # the rst parser...
    with open(input_fn, 'r') as f:
        md = f.read()
    lines = md.split('\n')
    in_code = CharInMDCode(lines)
    for i, line in enumerate(lines):
        m = mark_re_md.match(line)
        if (m is not None
            and m[1] not in ('ref', 'numref', 'eqref')
            and not in_code.in_code(i,0)
            and m.end() == len(line)):
            lines[i] = '\n'+line+'\n'
    reader = notedown.MarkdownReader(match='strict')
    notebook= reader.reads('\n'.join(lines))
    # evaluate
    if run_cells:
        # change to the notebook directory to resolve the relpaths properly
        cwd = os.getcwd()
        os.chdir(os.path.join(cwd, os.path.dirname(output_fn)))
        notedown.run(notebook, timeout)
        os.chdir(cwd)
    # write
    notebook['metadata'].update({'language_info':{'name':lang}})
    with open(output_fn, 'w') as f:
        f.write(nbformat.writes(notebook))

def delete_lines(lines, deletes):
    return [line for i, line in enumerate(lines) if i not in deletes]

class CharInMDCode(object):
    # indicating if a char is in a code block in markdown
    def __init__(self, lines):
        in_code = []
        code_block_mark = None
        for line in lines:
            if not code_block_mark:
                if self._get_code_block_mark(line):
                    code_block_mark = self._get_code_block_mark(line)
                    in_code.append([True]*len(line))
                else:
                    char_in_code = False
                    code_line = [False] * len(line)
                    for i, char in enumerate(line):
                        if char == '`':
                            char_in_code ^= True
                        if char_in_code:
                            code_line[i] = True
                    in_code.append(code_line)
            else:
                in_code.append([True]*len(line))
                if line.strip().startswith(code_block_mark):
                    code_block_mark = None
        self._in_code = in_code

    def _match_back_quote(self, line):
        mark = ''
        for char in line:
            if char == '`':
                mark += '`'
            else:
                break
        return mark

    def _get_code_block_mark(self, line):
        ls = line.strip()
        if ls.startswith('```'):
            return self._match_back_quote(ls)
        return None

    def in_code(self, line_i, pos):
        return self._in_code[line_i][pos]

def process_rst(body):
    def indented(line):
        return line.startswith('   ')
    def blank(line):
        return len(line.strip()) == 0

    def look_behind(i, cond):
        indices = []
        while i < n and cond(lines[i]):
            indices.append(i)
            i = i + 1
        return indices
    lines = body.split('\n')
    i, n, deletes = 0, len(lines), []
    while i < n:
        line = lines[i]
        if line.startswith('.. code:: toc'):
            # convert into rst's toc block
            lines[i] = '.. toctree::'
            blanks = look_behind(i+1, blank)
            deletes.extend(blanks)
            i += len(blanks)
        elif line.startswith('.. code:: eval_rst'):
            # make it a rst block
            deletes.append(i)
            for j in range(i+1, len(lines)):
                line_j = lines[j]
                if indented(line_j):
                    lines[j] = line_j[3:]
                elif not blank(line_j):
                    break
            i = j
        elif line.startswith('.. parsed-literal::'):
            # add a output class so we can add customized css
            lines[i] += '\n    :class: output'
            i += 1
        elif indented(line) and ':alt:' in line:
            # Image caption, remove :alt: block, it cause trouble for long captions
            caps = look_behind(i, lambda l: indented(l) and not blank(l))
            deletes.extend(caps)
            i += len(caps)
        elif line.startswith('.. table::'):
            # Add indent to table caption for long captions
            caps = look_behind(i+1, lambda l: not indented(l) and not blank(l))
            for j in caps:
                lines[j] = '   ' + lines[j]
            i += len(caps) + 1
        else:
            i += 1

    # change :label:my_label: into rst format
    lines = delete_lines(lines, deletes)
    deletes = []

    in_code = CharInMDCode(lines)
    for i, line in enumerate(lines):
        pos, new_line = 0, ''
        while True:
            match = mark_re.search(line, pos)
            if match is None:
                new_line += line[pos:]
                break
            start, end = match.start(), match.end()
            origin, key, value = match[0], match[1], match[2]
            new_line += line[pos:start]
            pos = end
            if in_code.in_code(i, start):
                new_line += origin # no change if in code
            else:
               # assert key in ['label', 'eqlabel', 'ref', 'numref', 'eqref', 'width', 'height'], 'unknown key: ' + key
                if key == 'label':
                    new_line += '.. _' + value + ':'
                elif key in ['ref', 'numref', 'cite']:
                    new_line += ':'+key+':`'+value+'`'
                elif key == 'eqref':
                    new_line += ':eq:`'+value+'`'
                elif key == 'eqlabel':
                    new_line += '   :label: '+value
                    if blank(lines[i-1]):
                        deletes.append(i-1)
                elif key in ['width', 'height']:
                    new_line += '   :'+key+': '+value
                elif key == 'bibliography':
                    # a hard coded plain bibtex style...
                    new_line += ('.. bibliography:: ' + value +
                                 '\n   :style: plain\n   :all:')
                else:
                    logging.fatal('unknown key', key)

        lines[i] = new_line
    lines = delete_lines(lines, deletes)

    def move(i, j): # move line i to line j
        lines.insert(j, lines[i])
        if i > j:
            del lines[i+1]
        else:
            del lines[i]

    # move :width: or :width: just below .. figure::
    for i, line in enumerate(lines):
        if line.startswith('.. figure::'):
            for j in range(i+1, len(lines)):
                line_j = lines[j]
                if not indented(line_j) and not blank(line_j):
                    break
                if line_j.startswith('   :width:') or line_j.startswith('   :height:'):
                    move(j, i+1)

    # move .. _label: before a image, a section, or a table
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('.. _'):
            for j in range(i-1, -1, -1):
                line_j = lines[j]
                if (line_j.startswith('.. table:')
                    or line_j.startswith('.. figure:')):
                    move(i, j-1)
                    lines.insert(j-1, '')
                    i += 1
                    break
                if (len(set(line_j)) == 1
                    and line_j[0] in ['=','~','_', '-']):
                    move(i, j-2)
                    lines.insert(j-2, '')
                    i += 1
                    break
        i += 1

    return '\n'.join(lines)

def ipynb2rst(input_fn, output_fn):
    with open(input_fn, 'r') as f:
        notebook = nbformat.read(f, as_version=4)
    writer = nbconvert.RSTExporter()
    sig = hashlib.sha1(input_fn.encode()).hexdigest()[:6]
    resources = {'unique_key':
                 'output_'+rm_ext(os.path.basename(output_fn))+'_'+sig}
    (body, resources) = writer.from_notebook_node(notebook, resources)

    body = process_rst(body)

    with open(output_fn, 'w') as f:
        f.write(body)
    outputs = resources['outputs']
    base_dir = os.path.dirname(output_fn)
    for fn in outputs:
        full_fn = os.path.join(base_dir, fn)
        with open(full_fn, 'wb') as f:
            f.write(outputs[fn])
