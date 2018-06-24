# Copyright 2018 The YARL-Project, All Rights Reserved.
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

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from yarl import YARLError
from yarl.components import Component


class FixedLoop(Component):
    """
    A FixedLoop component is used to iteratively call other GraphFunctions, e.g. in an optimization.
    """
    def __init__(self, num_iterations, call_component, graph_fn_name, scope="fixed-loop", **kwargs):
        """
        Args:
            num_iterations (int): How often to call the given GraphFn.
            call_component (Component): Component providing graph fn to call within loop.
            graph_fn_name (str): The name of the graph_fn in call_component.
        """
        assert num_iterations > 0

        super(FixedLoop, self).__init__(scope=scope, **kwargs)

        self.num_iterations = num_iterations
        self.graph_fn_to_call = None
        for graph_fn in call_component.graph_fns:
            if graph_fn.name == graph_fn_name:
                self.graph_fn_to_call = graph_fn
                break
        if not self.graph_fn_to_call:
            raise YARLError("ERROR: GraphFn '{}' not found in Component '{}'!".format(graph_fn_name,
                                                                                      call_component.global_scope))
        # TODO: Do we sum up, append to list, ...?
        self.define_outputs("fixed_loop_result")
        self.add_component(call_component)

        self.add_graph_fn()  # TODO:

    def _graph_fn_call_loop(self, *params):
        ret = None
        for _ in range(self.num_iterations):
            ret = self.graph_fn_to_call(*params)
        # For now, just return last result.
        return ret