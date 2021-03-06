# Copyright 2017 Google Inc. All Rights Reserved.
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
# ==============================================================================

"""Adds support for parameterized tests to Python's unittest TestCase class.

A parameterized test is a method in a test case that is invoked with different
argument tuples.

A simple example:

  class AdditionExample(parameterized.ParameterizedTestCase):
    @parameterized.Parameters(
       (1, 2, 3),
       (4, 5, 9),
       (1, 1, 3))
    def testAddition(self, op1, op2, result):
      self.assertEqual(result, op1 + op2)


Each invocation is a separate test case and properly isolated just
like a normal test method, with its own setUp/tearDown cycle. In the
example above, there are three separate testcases, one of which will
fail due to an assertion error (1 + 1 != 3).

Parameters for invididual test cases can be tuples (with positional parameters)
or dictionaries (with named parameters):

  class AdditionExample(parameterized.ParameterizedTestCase):
    @parameterized.Parameters(
       {'op1': 1, 'op2': 2, 'result': 3},
       {'op1': 4, 'op2': 5, 'result': 9},
    )
    def testAddition(self, op1, op2, result):
      self.assertEqual(result, op1 + op2)

If a parameterized test fails, the error message will show the
original test name (which is modified internally) and the arguments
for the specific invocation, which are part of the string returned by
the shortDescription() method on test cases.

The id method of the test, used internally by the unittest framework,
is also modified to show the arguments. To make sure that test names
stay the same across several invocations, object representations like

  >>> class Foo(object):
  ...  pass
  >>> repr(Foo())
  '<__main__.Foo object at 0x23d8610>'

are turned into '<__main__.Foo>'. For even more descriptive names,
especially in test logs, you can use the NamedParameters decorator. In
this case, only tuples are supported, and the first parameters has to
be a string (or an object that returns an apt name when converted via
str()):

  class NamedExample(parameterized.ParameterizedTestCase):
    @parameterized.NamedParameters(
       ('Normal', 'aa', 'aaa', True),
       ('EmptyPrefix', '', 'abc', True),
       ('BothEmpty', '', '', True))
    def testStartsWith(self, prefix, string, result):
      self.assertEqual(result, string.startswith(prefix))

Named tests also have the benefit that they can be run individually
from the command line:

  $ testmodule.py NamedExample.testStartsWithNormal
  .
  --------------------------------------------------------------------
  Ran 1 test in 0.000s

  OK

Parameterized Classes
=====================
If invocation arguments are shared across test methods in a single
ParameterizedTestCase class, instead of decorating all test methods
individually, the class itself can be decorated:

  @parameterized.Parameters(
    (1, 2, 3),
    (4, 5, 9))
  class ArithmeticTest(parameterized.ParameterizedTestCase):
    def testAdd(self, arg1, arg2, result):
      self.assertEqual(arg1 + arg2, result)

    def testSubtract(self, arg1, arg2, result):
      self.assertEqual(result - arg1, arg2)

Inputs from Iterables
=====================
If parameters should be shared across several test cases, or are dynamically
created from other sources, a single non-tuple iterable can be passed into
the decorator. This iterable will be used to obtain the test cases:

  class AdditionExample(parameterized.ParameterizedTestCase):
    @parameterized.Parameters(
      c.op1, c.op2, c.result for c in testcases
    )
    def testAddition(self, op1, op2, result):
      self.assertEqual(result, op1 + op2)


Single-Argument Test Methods
============================
If a test method takes only one argument, the single arguments must not be
wrapped into a tuple:

  class NegativeNumberExample(parameterized.ParameterizedTestCase):
    @parameterized.Parameters(
       -1, -3, -4, -5
    )
    def testIsNegative(self, arg):
      self.assertTrue(IsNegative(arg))


List/tuple as a Single Argument
===============================
If a test method takes a single argument of a list/tuple, it must be wrapped
inside a tuple:

  class ZeroSumExample(parameterized.ParameterizedTestCase):
    @parameterized.Parameters(
      ([-1, 0, 1], ),
      ([-2, 0, 2], ),
    )
    def testSumIsZero(self, arg):
      self.assertEqual(0, sum(arg))
"""

import collections
import functools
import re
import types
import unittest
import uuid

from tensorflow.python.platform import googletest

ADDR_RE = re.compile(r'\<([a-zA-Z0-9_\-\.]+) object at 0x[a-fA-F0-9]+\>')
_SEPARATOR = uuid.uuid1().hex
_FIRST_ARG = object()
_ARGUMENT_REPR = object()


def _CleanRepr(obj):
  return ADDR_RE.sub(r'<\1>', repr(obj))


# Helper function formerly from the unittest module, removed from it in
# Python 2.7.
def _StrClass(cls):
  return '%s.%s' % (cls.__module__, cls.__name__)


def _NonStringIterable(obj):
  return (isinstance(obj, collections.Iterable) and not
          isinstance(obj, basestring))


def _FormatParameterList(testcase_params):
  if isinstance(testcase_params, collections.Mapping):
    return ', '.join('%s=%s' % (argname, _CleanRepr(value))
                     for argname, value in testcase_params.iteritems())
  elif _NonStringIterable(testcase_params):
    return ', '.join(map(_CleanRepr, testcase_params))
  else:
    return _FormatParameterList((testcase_params,))


class _ParameterizedTestIter(object):
  """Callable and iterable class for producing new test cases."""

  def __init__(self, test_method, testcases, naming_type):
    """Returns concrete test functions for a test and a list of parameters.

    The naming_type is used to determine the name of the concrete
    functions as reported by the unittest framework. If naming_type is
    _FIRST_ARG, the testcases must be tuples, and the first element must
    have a string representation that is a valid Python identifier.

    Args:
      test_method: The decorated test method.
      testcases: (list of tuple/dict) A list of parameter
                 tuples/dicts for individual test invocations.
      naming_type: The test naming type, either _NAMED or _ARGUMENT_REPR.
    """
    self._test_method = test_method
    self.testcases = testcases
    self._naming_type = naming_type
    self.__name__ = _ParameterizedTestIter.__name__

  def __call__(self, *args, **kwargs):
    raise RuntimeError('You appear to be running a parameterized test case '
                       'without having inherited from parameterized.'
                       'ParameterizedTestCase. This is bad because none of '
                       'your test cases are actually being run. You may also '
                       'be using a mock annotation before the parameterized '
                       'one, in which case you should reverse the order.')

  def __iter__(self):
    test_method = self._test_method
    naming_type = self._naming_type

    def MakeBoundParamTest(testcase_params):
      @functools.wraps(test_method)
      def BoundParamTest(self):
        if isinstance(testcase_params, collections.Mapping):
          test_method(self, **testcase_params)
        elif _NonStringIterable(testcase_params):
          test_method(self, *testcase_params)
        else:
          test_method(self, testcase_params)

      if naming_type is _FIRST_ARG:
        # Signal the metaclass that the name of the test function is unique
        # and descriptive.
        BoundParamTest.__x_use_name__ = True

        # Support PEP-8 underscore style for test naming if used.
        if (BoundParamTest.__name__.startswith('test_')
            and testcase_params[0]
            and not testcase_params[0].startswith('_')):
          BoundParamTest.__name__ += '_'

        BoundParamTest.__name__ += str(testcase_params[0])
        testcase_params = testcase_params[1:]
      elif naming_type is _ARGUMENT_REPR:
        # __x_extra_id__ is used to pass naming information to the __new__
        # method of TestGeneratorMetaclass.
        # The metaclass will make sure to create a unique, but nondescriptive
        # name for this test.
        BoundParamTest.__x_extra_id__ = '(%s)' % (
            _FormatParameterList(testcase_params),)
      else:
        raise RuntimeError('%s is not a valid naming type.' % (naming_type,))

      BoundParamTest.__doc__ = '%s(%s)' % (
          BoundParamTest.__name__, _FormatParameterList(testcase_params))
      if test_method.__doc__:
        BoundParamTest.__doc__ += '\n%s' % (test_method.__doc__,)
      return BoundParamTest
    return (MakeBoundParamTest(c) for c in self.testcases)


def _IsSingletonList(testcases):
  """True iff testcases contains only a single non-tuple element."""
  return len(testcases) == 1 and not isinstance(testcases[0], tuple)


def _ModifyClass(class_object, testcases, naming_type):
  assert not getattr(class_object, '_id_suffix', None), (
      'Cannot add parameters to %s,'
      ' which already has parameterized methods.' % (class_object,))
  class_object._id_suffix = id_suffix = {}
  for name, obj in class_object.__dict__.items():
    if (name.startswith(unittest.TestLoader.testMethodPrefix)
        and isinstance(obj, types.FunctionType)):
      delattr(class_object, name)
      methods = {}
      _UpdateClassDictForParamTestCase(
          methods, id_suffix, name,
          _ParameterizedTestIter(obj, testcases, naming_type))
      for name, meth in methods.iteritems():
        setattr(class_object, name, meth)


def _ParameterDecorator(naming_type, testcases):
  """Implementation of the parameterization decorators.

  Args:
    naming_type: The naming type.
    testcases: Testcase parameters.

  Returns:
    A function for modifying the decorated object.
  """
  def _Apply(obj):
    if isinstance(obj, type):
      _ModifyClass(
          obj,
          list(testcases) if not isinstance(testcases, collections.Sequence)
          else testcases,
          naming_type)
      return obj
    else:
      return _ParameterizedTestIter(obj, testcases, naming_type)

  if _IsSingletonList(testcases):
    assert _NonStringIterable(testcases[0]), (
        'Single parameter argument must be a non-string iterable')
    testcases = testcases[0]

  return _Apply


def Parameters(*testcases):
  """A decorator for creating parameterized tests.

  See the module docstring for a usage example.
  Args:
    *testcases: Parameters for the decorated method, either a single
                iterable, or a list of tuples/dicts/objects (for tests
                with only one argument).

  Returns:
     A test generator to be handled by TestGeneratorMetaclass.
  """
  return _ParameterDecorator(_ARGUMENT_REPR, testcases)


def NamedParameters(*testcases):
  """A decorator for creating parameterized tests.

  See the module docstring for a usage example. The first element of
  each parameter tuple should be a string and will be appended to the
  name of the test method.

  Args:
    *testcases: Parameters for the decorated method, either a single
                iterable, or a list of tuples.

  Returns:
     A test generator to be handled by TestGeneratorMetaclass.
  """
  return _ParameterDecorator(_FIRST_ARG, testcases)


class TestGeneratorMetaclass(type):
  """Metaclass for test cases with test generators.

  A test generator is an iterable in a testcase that produces callables. These
  callables must be single-argument methods. These methods are injected into
  the class namespace and the original iterable is removed. If the name of the
  iterable conforms to the test pattern, the injected methods will be picked
  up as tests by the unittest framework.

  In general, it is supposed to be used in conjuction with the
  Parameters decorator.
  """

  def __new__(mcs, class_name, bases, dct):
    dct['_id_suffix'] = id_suffix = {}
    for name, obj in dct.items():
      if (name.startswith(unittest.TestLoader.testMethodPrefix) and
          _NonStringIterable(obj)):
        iterator = iter(obj)
        dct.pop(name)
        _UpdateClassDictForParamTestCase(dct, id_suffix, name, iterator)

    return type.__new__(mcs, class_name, bases, dct)


def _UpdateClassDictForParamTestCase(dct, id_suffix, name, iterator):
  """Adds individual test cases to a dictionary.

  Args:
    dct: The target dictionary.
    id_suffix: The dictionary for mapping names to test IDs.
    name: The original name of the test case.
    iterator: The iterator generating the individual test cases.
  """
  for idx, func in enumerate(iterator):
    assert callable(func), 'Test generators must yield callables, got %r' % (
        func,)
    if getattr(func, '__x_use_name__', False):
      new_name = func.__name__
    else:
      new_name = '%s%s%d' % (name, _SEPARATOR, idx)
    assert new_name not in dct, (
        'Name of parameterized test case "%s" not unique' % (new_name,))
    dct[new_name] = func
    id_suffix[new_name] = getattr(func, '__x_extra_id__', '')


class ParameterizedTestCase(googletest.TestCase):
  """Base class for test cases using the Parameters decorator."""
  __metaclass__ = TestGeneratorMetaclass

  def _OriginalName(self):
    return self._testMethodName.split(_SEPARATOR)[0]

  def __str__(self):
    return '%s (%s)' % (self._OriginalName(), _StrClass(self.__class__))

  def id(self):  # pylint: disable=invalid-name
    """Returns the descriptive ID of the test.

    This is used internally by the unittesting framework to get a name
    for the test to be used in reports.

    Returns:
      The test id.
    """
    return '%s.%s%s' % (_StrClass(self.__class__),
                        self._OriginalName(),
                        self._id_suffix.get(self._testMethodName, ''))
