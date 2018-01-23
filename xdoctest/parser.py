# -*- coding: utf-8 -*-
"""
Terms and definitions:

    logical block: a snippet of code that can be executed by itself if given
        the correct global / local variable context.

    PS1 : The original meaning is "Prompt String 1". In the context of
        xdoctest, instead of refering to the prompt prefix, we use PS1 to refer
        to a line that starts a "logical block" of code. In the original
        doctest module these all had to be prefixed with ">>>". In xdoctest the
        prefix is used to simply denote the code is part of a doctest. It does
        not necessarilly mean a new "logical block" is starting.

    PS2 : The original meaning is "Prompt String 2". In the context of
        xdoctest, instead of refering to the prompt prefix, we use PS2 to refer
        to a line that continues a "logical block" of code. In the original
        doctest module these all had to be prefixed with "...". However,
        xdoctest uses parsing to automatically determine this.

    want statement: Lines directly after a logical block of code in a doctest
        indicating the desired result of executing the previous block.
"""
from __future__ import print_function, division, absolute_import, unicode_literals
import six
import ast
import sys
import math
import re
import itertools as it
from xdoctest import utils
from xdoctest import checker
from xdoctest import directive
from xdoctest import exceptions
from xdoctest import static_analysis as static


GotWantException = checker.GotWantException


INDENT_RE = re.compile('^([ ]*)(?=\S)', re.MULTILINE)


_EXCEPTION_RE = re.compile(r"""
    # Grab the traceback header.  Different versions of Python have
    # said different things on the first traceback line.
    ^(?P<hdr> Traceback\ \(
        (?: most\ recent\ call\ last
        |   innermost\ last
        ) \) :
    )
    \s* $                # toss trailing whitespace on the header.
    (?P<stack> .*?)      # don't blink: absorb stuff until...
    ^ (?P<msg> \w+ .*)   #     a line *starts* with alphanum.
    """, re.VERBOSE | re.MULTILINE | re.DOTALL)


class DoctestPart(object):
    """
    The result of parsing that represents a "logical block" of code.
    If a want statment is defined, it is stored here.
    """
    def __init__(self, exec_lines, want_lines=None, line_offset=0,
                 orig_lines=None, directives=None):
        self.exec_lines = exec_lines
        self.want_lines = want_lines
        self.line_offset = line_offset
        self.orig_lines = orig_lines
        self.use_eval = False
        self._directives = directives

    @property
    def n_lines(self):
        return self.n_exec_lines + self.n_want_lines

    @property
    def n_exec_lines(self):
        return len(self.exec_lines)

    @property
    def n_want_lines(self):
        if self.want_lines:
            return len(self.want_lines)
        else:
            return 0

    @property
    def source(self):
        return '\n'.join(self.exec_lines)

    @property
    def directives(self):
        """
        CommandLine:
            python -m xdoctest.parser DoctestPart.directives

        Example:
            >>> self = DoctestPart(['# doctest: +SKIP'], None, 0)
            >>> print(', '.join(list(map(str, self.directives))))
            <Directive(+SKIP)>
        """
        if self._directives is None:
            self._directives = list(directive.extract(self.source))
        return self._directives

    @property
    def want(self):
        # options = self._find_options(source, name, lineno + s1)
        # example = DoctestPart(source, None, None, lineno=lineno + s1,
        #                       indent=indent, options=options)
        # the last part has a want
        # todo: If `want` contains a traceback message, then extract it.
        # m = _EXCEPTION_RE.match(want)
        # exc_msg = m.group('msg') if m else None
        if self.want_lines:
            return '\n'.join(self.want_lines)
        else:
            return None

    def __nice__(self):
        parts = []
        if self.line_offset is not None:
            parts.append('ln %s' % (self.line_offset))
        if self.source:
            head_src = self.source.splitlines()[0][0:8]
            parts.append('src="%s..."' % (head_src,))
        else:
            parts.append('src=""')

        if self.want is None:
            parts.append('want=None')
        else:
            head_wnt = self.want.splitlines()[0][0:8]
            parts.append('want="%s..."' % (head_wnt,))
        return ', '.join(parts)

    def __repr__(self):
        classname = self.__class__.__name__
        devnice = self.__nice__()
        return '<%s(%s) at %s>' % (classname, devnice, hex(id(self)))

    def __str__(self):
        classname = self.__class__.__name__
        devnice = self.__nice__()
        return '<%s(%s)>' % (classname, devnice)

    def check_got_vs_want(part, got_stdout, got_eval, not_evaled):
        # If we did not want anything than ignore eval and stdout
        if got_eval is not_evaled:
            # if there was no eval, check stdout
            got = got_stdout
            flag = checker.check_output(got, part.want)
        else:
            if not got_stdout:
                # If there was no stdout then use eval value.
                got = repr(got_eval)
                flag = checker.check_output(got, part.want)
            else:
                # If there was eval and stdout, defer to stdout
                # but allow fallback on the eval.
                got = got_stdout
                flag = checker.check_output(got, part.want)
                if not flag:
                    # allow eval to fallback and save us, but if it fails, do a
                    # diff with stdout
                    got = repr(got_eval)
                    flag = checker.check_output(got, part.want)
                    if not flag:
                        got = got_stdout
        if not flag:
            # print('got = {!r}'.format(got))
            # print('part.want = {!r}'.format(part.want))
            # msg += output_difference(part.want, got)
            got, want = checker.normalize(got, part.want)
            msg = 'got differs with doctest want'
            ex = checker.GotWantException(msg, got, want)
            raise ex

    def format_src(self, linenos=True, want=True, startline=1, n_digits=None,
                   colored=False):
        """
        Customizable formatting of the source and want for this doctest.

        Args:
            linenos (bool): show line numbers
            want (bool): include the want value if it exists
            startline (int): offsets the line numbering
            n_digits (int): number of digits to use for line numbers
            colored (bool): pygmentize the colde

        Example:
            >>> from xdoctest.parser import *
            >>> self = DoctestPart(['print(123)'], ['123'], 0)
            >>> print(self.format_src())
            1 >>> print(123)
              123
        """
        src_text = self.source
        src_text = utils.indent(src_text, '>>> ')
        want_text = self.want if self.want else ''

        if n_digits is None:
            endline = startline + self.n_lines
            n_digits = math.log(max(1, endline), 10)
            n_digits = int(math.ceil(n_digits))

        if linenos:
            src_fmt = '{{:{}d}} {{}}'.format(n_digits)
            want_fmt = '{} {{}}'.format(' ' * n_digits)

            new_lines = []
            count = startline + self.line_offset
            for count, line in enumerate(src_text.splitlines(), start=count):
                new_lines.append(src_fmt.format(count, line))
            if want_text:
                for count, line in enumerate(want_text.splitlines(), start=count):
                    if want:
                        new_lines.append(want_fmt.format(line))
            part_text = '\n'.join(new_lines)
        else:
            if want_text:
                part_text = src_text
                if want:
                    part_text = part_text + '\n' + want_text
            else:
                part_text = src_text
        if colored:
            part_text = utils.highlight_code(part_text, 'python')
        return part_text


class DoctestParser(object):
    r"""
    Breaks docstrings into parts useing the `parse` method.

    Example:
        >>> parser = DoctestParser()
        >>> doctest_parts = parser.parse(
        >>>     '''
        >>>     >>> j = 0
        >>>     >>> for i in range(10):
        >>>     >>>     j += 1
        >>>     >>> print(j)
        >>>     10
        >>>     '''.lstrip('\n'))
        >>> print('\n'.join(list(map(str, doctest_parts))))
        <DoctestPart(ln 0, src="j = 0...", want=None)>
        <DoctestPart(ln 3, src="print(j)...", want="10...")>
    """

    def __init__(self, simulate_repl=False):
        self.simulate_repl = simulate_repl

    def parse(self, string, info=None):
        """
        Divide the given string into examples and intervening text.

        Args:
            string (str): string representing the doctest
            info (dict): info about where the string came from in case of an
                error

        Returns:
            list : a list of `DoctestPart` objects

        CommandLine:
            python -m xdoctest.parser DoctestParser.parse

        Example:
            >>> s = 'I am a dummy example with two parts'
            >>> x = 10
            >>> print(s)
            I am a dummy example with two parts
            >>> s = 'My purpose it so demonstrate how wants work here'
            >>> print('The new want applies ONLY to stdout')
            >>> print('given before the last want')
            >>> '''
                this wont hurt the test at all
                even though its multiline '''
            >>> y = 20
            The new want applies ONLY to stdout
            given before the last want
            >>> # Parts from previous examples are executed in the same context
            >>> print(x + y)
            30

            this is simply text, and doesnt apply to the previous doctest the
            <BLANKLINE> directive is still in effect.

        Example:
            >>> from xdoctest import parser
            >>> from xdoctest import docscrape_google
            >>> from xdoctest import core
            >>> self = parser.DoctestParser()
            >>> docstr = self.parse.__doc__
            >>> blocks = docscrape_google.split_google_docblocks(docstr)
            >>> doclineno = self.parse.__func__.__code__.co_firstlineno
            >>> key, (string, offset) = blocks[-2]
            >>> self._label_docsrc_lines(string)
            >>> doctest_parts = self.parse(string)
            >>> # each part with a want-string needs to be broken in two
            >>> assert len(doctest_parts) == 6
        """
        if sys.version_info.major == 2:  # nocover
            string = utils.ensure_unicode(string)

        if not isinstance(string, six.string_types):
            raise TypeError('Expected string but got {!r}'.format(string))

        string = string.expandtabs()
        # If all lines begin with the same indentation, then strip it.
        min_indent = min_indentation(string)
        if min_indent > 0:
            string = '\n'.join([l[min_indent:] for l in string.splitlines()])

        try:
            labeled_lines = self._label_docsrc_lines(string)
            grouped_lines = self._group_labeled_lines(labeled_lines)

            all_parts = list(self._package_groups(grouped_lines))
        except Exception as orig_ex:
            # print('Failed to parse string=...')
            # print(string)
            # print(info)
            raise exceptions.DoctestParseError('Failed to parse doctest',
                                               string=string, info=info,
                                               orig_ex=orig_ex)
        return all_parts

    def _package_groups(self, grouped_lines):
        lineno = 0
        for chunk in grouped_lines:
            if isinstance(chunk, tuple):
                slines, wlines = chunk
                for example in self._package_chunk(slines, wlines, lineno):
                    yield example
                lineno += len(slines) + len(wlines)
            else:
                text_part = '\n'.join(chunk)
                yield text_part
                lineno += len(chunk)

    def _package_chunk(self, raw_source_lines, raw_want_lines, lineno=0):
        """
        if `self.simulate_repl` is True, then each statment is broken into its
        own part.  Otherwise, statements are grouped by the closest `want`
        statement.

        Example:
            >>> from xdoctest.parser import *
            >>> raw_source_lines = ['>>> "string"']
            >>> raw_want_lines = ['string']
            >>> self = DoctestParser()
            >>> part, = self._package_chunk(raw_source_lines, raw_want_lines)
            >>> part.source
            '"string"'
            >>> part.want
            'string'

        """
        match = INDENT_RE.search(raw_source_lines[0])
        line_indent = 0 if match is None else (match.end() - match.start())

        source_lines = [p[line_indent:] for p in raw_source_lines]
        want_lines = [p[line_indent:] for p in raw_want_lines]

        exec_source_lines = [p[4:] for p in source_lines]

        # Find the line number of each standalone statment
        ps1_linenos, eval_final = self._locate_ps1_linenos(source_lines)

        # Find all directives here:
        # A directive necessarilly will split a doctest into multiple parts
        # There are two types: block directives and inline-directives
        # First find block directives which must exist on there own PS1 line
        break_linenos = []
        line_to_directives = {}
        for s1 in ps1_linenos:
            line = exec_source_lines[s1]
            directives = list(directive.extract(line))
            if directives:
                break_linenos.append(s1)
                line_to_directives[s1] = directives

        for s1, s2 in zip(ps1_linenos, ps1_linenos[1:] + [None]):
            if s1 not in break_linenos:
                lines = exec_source_lines[s1:s2]
                directives = list(directive.extract('\n'.join(lines)))
                if directives:
                    break_linenos.append(s1)
                    line_to_directives[s1] = directives
                    if s2 is not None:
                        break_linenos.append(s2)

        def slice_example(s1, s2, want_lines=None):
            exec_lines = exec_source_lines[s1:s2]
            orig_lines = source_lines[s1:s2]
            directives = line_to_directives.get(s1, None)
            example = DoctestPart(exec_lines, want_lines=want_lines,
                                  orig_lines=orig_lines,
                                  line_offset=lineno + s1,
                                  directives=directives)
            return example

        s1 = 0
        s2 = 0
        if self.simulate_repl:
            # Break down first parts which dont have any want
            for s1, s2 in zip(ps1_linenos, ps1_linenos[1:]):
                example = slice_example(s1, s2)
                yield example
            s1 = s2
        else:
            if break_linenos:
                break_linenos = sorted(set([0] + break_linenos))
                # directives are forcing us to further breakup the parts
                for s1, s2 in zip(break_linenos, break_linenos[1:]):
                    example = slice_example(s1, s2)
                    yield example
                s1 = s2
            if want_lines and eval_final:
                # Whenever the evaluation of the final line needs to be tested
                # against want, that line must be separated into its own part.
                # We break the last line off so we can eval its value, but keep
                # previous groupings.
                s2 = ps1_linenos[-1]
                if s2 != s1:  # make sure the last line is not the only line
                    example = slice_example(s1, s2)
                    yield example
                    s1 = s2
        s2 = None

        example = slice_example(s1, s2, want_lines)
        example.use_eval = bool(want_lines) and eval_final
        yield example

    def _group_labeled_lines(self, labeled_lines):
        # Now that lines have types, group them. This could have done this
        # above, but functinoality is split for readability.
        prev_source = None
        grouped_lines = []
        for state, group in it.groupby(labeled_lines, lambda t: t[0]):
            block = [t[1] for t in group]
            if state == 'text':
                if prev_source is not None:
                    # accept a source block without a want block
                    grouped_lines.append((prev_source, ''))
                    prev_source = None
                # accept the text
                grouped_lines.append(block)
            elif state == 'want':
                assert prev_source is not None, 'impossible'
                grouped_lines.append((prev_source, block))
                prev_source = None
            elif state == 'dsrc':
                # need to check if there is a want after us
                prev_source = block
        # Case where last block is source
        if prev_source:
            grouped_lines.append((prev_source, ''))
        return grouped_lines

    def _locate_ps1_linenos(self, source_lines):
        """
        Determines which lines in the source begin a "logical block" of code.

        Args:
            source_lines (list): lines belonging only to the doctest src
                these will be unindented, prefixed, and without any want.

        Example:
            >>> self = DoctestParser()
            >>> source_lines = ['>>> def foo():', '>>>     return 0', '>>> 3']
            >>> linenos, eval_final = self._locate_ps1_linenos(source_lines)
            >>> assert linenos == [0, 2]
            >>> assert eval_final is True

        Example:
            >>> self = DoctestParser()
            >>> source_lines = ['>>> x = [1, 2, ', '>>> 3, 4]', '>>> print(len(x))']
            >>> linenos, eval_final = self._locate_ps1_linenos(source_lines)
            >>> assert linenos == [0, 2]
            >>> assert eval_final is True
        """
        # print('source_lines = {!r}'.format(source_lines))
        # Strip indentation (and PS1 / PS2 from source)
        exec_source_lines = [p[4:] for p in source_lines]

        # Hack to make comments appear like executable statements
        # note, this hack never leaves this function because we only are
        # returning line numbers.
        exec_source_lines = ['_._  = None' if p.startswith('#') else p
                             for p in exec_source_lines]

        source_block = '\n'.join(exec_source_lines)
        pt = ast.parse(source_block)
        statement_nodes = pt.body
        ps1_linenos = [node.lineno - 1 for node in statement_nodes]
        NEED_16806_WORKAROUND = True
        if NEED_16806_WORKAROUND:  # pragma: nobranch
            ps1_linenos = self._workaround_16806(
                ps1_linenos, exec_source_lines)
        # Respect any line explicitly defined as PS2 (via its prefix)
        ps2_linenos = {
            x for x, p in enumerate(source_lines) if p[:4] != '>>> '
        }
        ps1_linenos = sorted(ps1_linenos.difference(ps2_linenos))

        # Is the last statement evaluatable?
        if sys.version_info.major == 2:  # nocover
            eval_final = isinstance(statement_nodes[-1], (
                ast.Expr, ast.Print))
        else:
            # This should just be an Expr in python3
            # (todo: ensure this is true)
            eval_final = isinstance(statement_nodes[-1], ast.Expr)

        return ps1_linenos, eval_final

    def _workaround_16806(self, ps1_linenos, exec_source_lines):
        """
        workaround for python issue 16806 (https://bugs.python.org/issue16806)

        Issue causes lineno for multiline strings to give the line they end on,
        not the line they start on.  A patch for this issue exists
        `https://github.com/python/cpython/pull/1800`

        Notes:
            Starting from the end look at consecutive pairs of indices to
            inspect the statment it corresponds to.  (the first statment goes
            from ps1_linenos[-1] to the end of the line list.
        """
        new_ps1_lines = []
        b = len(exec_source_lines)
        for a in ps1_linenos[::-1]:
            # the position of `b` is correct, but `a` may be wrong
            # is_balanced_statement will be False iff `a` is wrong.
            while not static.is_balanced_statement(exec_source_lines[a:b]):
                # shift `a` down until it becomes correct
                a -= 1
            # push the new correct value back into the list
            new_ps1_lines.append(a)
            # set the end position of the next string to be `a` ,
            # note, because this `a` is correct, the next `b` is
            # must also be correct.
            b = a
        ps1_linenos = set(new_ps1_lines)
        return ps1_linenos

    def _label_docsrc_lines(self, string):
        """
        Example:
            >>> from xdoctest.parser import *
            >>> # Having multiline strings in doctests can be nice
            >>> string = utils.codeblock(
                    '''
                    text
                    >>> items = ['also', 'nice', 'to', 'not', 'worry',
                    >>>          'about', '...', 'vs', '>>>']
                    ... print('but its still allowed')
                    but its still allowed

                    more text
                    ''')
            >>> self = DoctestParser()
            >>> labeled = self._label_docsrc_lines(string)
            >>> expected = [
            >>>     ('text', 'text'),
            >>>     ('dsrc', ">>> items = ['also', 'nice', 'to', 'not', 'worry',"),
            >>>     ('dsrc', ">>>          'about', '...', 'vs', '>>>']"),
            >>>     ('dsrc', "... print('but its still allowed')"),
            >>>     ('want', 'but its still allowed'),
            >>>     ('text', ''),
            >>>     ('text', 'more text')
            >>> ]
            >>> assert labeled == expected
        """

        def _complete_source(line, state_indent, line_iter):
            """
            helper
            remove lines from the iterator if they are needed to complete source
            """

            norm_line = line[state_indent:]  # Normalize line indentation
            prefix = norm_line[:4]
            suffix = norm_line[4:]
            assert prefix.strip() in {'>>>', '...'}, '{}'.format(prefix)
            yield line

            source_parts = [suffix]
            while not static.is_balanced_statement(source_parts):
                try:
                    line_idx, next_line = next(line_iter)
                except StopIteration:
                    raise SyntaxError('ill-formed doctest')
                norm_line = next_line[state_indent:]
                prefix = norm_line[:4]
                suffix = norm_line[4:]
                if prefix.strip() not in {'>>>', '...', ''}:  # nocover
                    raise SyntaxError(
                        'Bad indentation in doctest on line {}: {!r}'.format(
                            line_idx, next_line))
                source_parts.append(suffix)
                yield next_line

        # parse and differenatiate between doctest source and want statements.
        labeled_lines = []
        state_indent = 0

        # line states
        TEXT = 'text'
        DSRC = 'dsrc'
        WANT = 'want'

        # Move through states, keeping track of points where states change
        #     text -> [text, dsrc]
        #     dsrc -> [dsrc, want, text]
        #     want -> [want, text, dsrc]
        prev_state = TEXT
        curr_state = None
        line_iter = enumerate(string.splitlines())
        for line_idx, line in line_iter:
            match = INDENT_RE.search(line)
            line_indent = 0 if match is None else (match.end() - match.start())
            norm_line = line[state_indent:]  # Normalize line indentation
            strip_line = line.strip()

            # Check prev_state transitions
            if prev_state == TEXT:
                # text transitions to source whenever a PS1 line is encountered
                # the PS1(>>>) can be at an arbitrary indentation
                if strip_line.startswith('>>> '):
                    curr_state = DSRC
                else:
                    curr_state = TEXT
            elif prev_state == WANT:
                # blank lines terminate wants
                if len(strip_line) == 0:
                    curr_state = TEXT
                # source-inconsistent indentation terminates want
                elif line.strip().startswith('>>> '):
                    curr_state = DSRC
                elif line_indent < state_indent:
                    curr_state = TEXT
                else:
                    curr_state = WANT
            elif prev_state == DSRC:  # pragma: nobranch
                if len(strip_line) == 0 or line_indent < state_indent:
                    curr_state = TEXT
                # allow source to continue with either PS1 or PS2
                elif norm_line.startswith(('>>> ', '... ')):
                    if strip_line == '...':
                        curr_state = WANT
                    else:
                        curr_state = DSRC
                else:
                    curr_state = WANT
            else:  # nocover
                # This should never happen
                raise AssertionError('Unknown state prev_state={}'.format(prev_state))

            # Handle transitions
            if prev_state != curr_state:
                # Handle start of new states
                if curr_state == TEXT:
                    state_indent = 0
                if curr_state == DSRC:
                    # Start a new source
                    state_indent = line_indent
                    # renormalize line when indentation changes
                    norm_line = line[state_indent:]

            # continue current state
            if curr_state == DSRC:
                # source parts may consume more than one line
                try:
                    for part in _complete_source(line, state_indent, line_iter):
                        labeled_lines.append((DSRC, part))
                except SyntaxError as orig_ex:
                    raise
                    # msg = ('SYNTAX ERROR WHEN PARSING DOCSTRING: \n')
                    # msg += string
                    # print(msg)
                    # ex = exceptions.DoctestParseError('Syntax Error',
                    #                                   string=string,
                    #                                   orig_ex=orig_ex)
                    # raise ex
                    # warnings.warn(msg)

            elif curr_state == WANT:
                labeled_lines.append((WANT, line))
            elif curr_state == TEXT:
                labeled_lines.append((TEXT, line))
            prev_state = curr_state

        return labeled_lines


def min_indentation(s):
    "Return the minimum indentation of any non-blank line in `s`"
    indents = [len(indent) for indent in INDENT_RE.findall(s)]
    if len(indents) > 0:
        return min(indents)
    else:
        return 0


if __name__ == '__main__':
    """
    CommandLine:
        python -m xdoctest.core
        python -m xdoctest.parser all
    """
    import xdoctest as xdoc
    xdoc.doctest_module()
