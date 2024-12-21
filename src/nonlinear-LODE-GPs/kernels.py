import copy as cp
from einops import rearrange
import re
import itertools
from itertools import zip_longest
from torch.distributions import constraints
import torch
from functools import reduce
import gpytorch
# from gpytorch.lazy import *
# from gpytorch.lazy.non_lazy_tensor import  lazify
from gpytorch.kernels.kernel import Kernel
from sage.all import *
import sage
#https://ask.sagemath.org/question/41204/getting-my-own-module-to-work-in-sage/
from sage.calculus.var import var
from sage.arith.misc import factorial
import numpy as np
import pdb
from gpytorch.constraints import Positive
import random
import einops
import pprint
from typing import List
torch_operations = {'mul': torch.mul, 'add': torch.add,
                    'pow': torch.pow, 'exp':torch.exp,
                    'sin':torch.sin, 'cos':torch.cos,
                    'log': torch.log}


DEBUG =False

class LODE_Kernel(Kernel):
        def __init__(self, A, common_terms, active_dims=None):
            super(LODE_Kernel, self).__init__(active_dims=active_dims)

            self.model_parameters = torch.nn.ParameterDict()
            #self.num_tasks = num_tasks

            D, U, V = A.smith_form()
            print(f"D:{D}")
            print(f"V:{V}")
            x, a, b = var("x, a, b")
            V_temp = [list(b) for b in V.rows()]
            #print(V_temp)
            V = sage_eval(f"matrix({str(V_temp)})", locals={"x":x, "a":a, "b":b})
            Vt = V.transpose()
            kernel_matrix, self.kernel_translation_dict, parameter_dict = create_kernel_matrix_from_diagonal(D)
            self.ode_count = A.nrows()
            self.kernelsize = len(kernel_matrix)
            self.model_parameters.update(parameter_dict)
            #print(self.model_parameters)
            x, dx1, dx2, t1, t2, *_ = var(["x", "dx1", "dx2"] + ["t1", "t2"] + [f"LODEGP_kernel_{i}" for i in range(len(kernel_matrix[Integer(0)]))])
            k = matrix(Integer(len(kernel_matrix)), Integer(len(kernel_matrix)), kernel_matrix)
            V = V.substitute(x=dx1)
            Vt = Vt.substitute(x=dx2)

            self.V = V
            self.matrix_multiplication = matrix(k.base_ring(), len(k[0]), len(k[0]), (V*k*Vt))
            self.diffed_kernel = differentiate_kernel_matrix(k, V, Vt, self.kernel_translation_dict)
            self.sum_diff_replaced = replace_sum_and_diff(self.diffed_kernel)
            self.covar_description = translate_kernel_matrix_to_gpytorch_kernel(self.sum_diff_replaced, self.model_parameters, common_terms=common_terms)

            self.num_tasks = len(self.covar_description)

        def num_outputs_per_input(self, x1, x2):
            """
            Given `n` data points `x1` and `m` datapoints `x2`, this multitask
            kernel returns an `(n*num_tasks) x (m*num_tasks)` covariance matrix.
            """
            return self.num_tasks

        #def forward(self, X, Z=None, common_terms=None):
        def forward(self, x1, x2, diag=False, **params):
            common_terms = params["common_terms"]
            model_parameters = self.model_parameters
            if not x2 is None:
                common_terms["t_diff"] = x1-x2.t()
                common_terms["t_sum"] = x1+x2.t()
                common_terms["t_ones"] = torch.ones_like(x1+x2.t())
                common_terms["t_zeroes"] = torch.zeros_like(x1+x2.t())
            K_list = list() 
            for rownum, row in enumerate(self.covar_description):
                for cell in row:
                    K_list.append(eval(cell))
            kernel_count = len(self.covar_description)
            # from https://discuss.pytorch.org/t/how-to-interleave-two-tensors-along-certain-dimension/11332/6
            #if K_list[0].ndim == 1:
            #    K_list = [kk.unsqueeze(1) for kk in K_list]
            K = einops.rearrange(K_list, '(t1 t2) h w -> (h t1) (w t2)', t1=kernel_count, t2=kernel_count)  

            if diag:
                return K.diag()
            return K 
        
        def __str__(self, substituted=False):
            if substituted:
                return pprint.pformat(str(self.sum_diff_replaced), indent=self.kernelsize)
            else:
                return pprint.pformat(str(self.diffed_kernel), indent=self.kernelsize)

        def __latexify_kernel__(self, substituted=False):
            if substituted:
                return pprint.pformat(latex(self.sum_diff_replaced), indent=self.kernelsize)
            else:
                return pprint.pformat(latex(self.diffed_kernel), indent=self.kernelsize)

        def __pretty_print_kernel__(self, substituted=False):
            if substituted:
                return pprint.pformat(pretty_print(self.matrix_multiplication), indent=self.kernelsize)
            else:
                pretty_print(self.matrix_multiplication)
                print(str(self.kernel_translation_dict))


def create_kernel_matrix_from_diagonal(D):
    t1, t2 = var("t1, t2")
    translation_dictionary = dict()
    param_dict = torch.nn.ParameterDict()
    #sage_covariance_matrix = [[0 for cell in range(max(len(D.rows()), len(D.columns())))] for row in range(max(len(D.rows()), len(D.columns())))]
    sage_covariance_matrix = [[0 for cell in range(len(D.columns()))] for row in range(len(D.columns()))]
    #for i in range(max(len(D.rows()), len(D.columns()))):
    for i in range(len(D.columns())):
        if i > len(D.diagonal())-1:
            entry = 0
        else:
            entry = D[i][i]
        var(f"LODEGP_kernel_{i}")
        if entry == 0:
            param_dict[f"signal_variance_{i}"] = torch.nn.Parameter(torch.tensor(float(0.)))
            param_dict[f"lengthscale_{i}"] = torch.nn.Parameter(torch.tensor(float(0.)))
            # Create an SE kernel
            var(f"signal_variance_{i}")
            var(f"lengthscale_{i}")
            translation_dictionary[f"LODEGP_kernel_{i}"] = globals()[f"signal_variance_{i}"]**2 * exp(-1/2*(t1-t2)**2/globals()[f"lengthscale_{i}"]**2)
        elif entry == 1:
            translation_dictionary[f"LODEGP_kernel_{i}"] = 0 
        else:
            kernel_translation_kernel = 0
            roots = entry.roots(ring=CC)
            roots_copy = cp.deepcopy(roots)
            for rootnum, root in enumerate(roots):
                # Complex root, i.e. sinusoidal exponential
                #if root[0].is_complex():
                param_dict[f"signal_variance_{i}_{rootnum}"] = torch.nn.Parameter(torch.tensor(float(0.)))
                var(f"signal_variance_{i}_{rootnum}")
                if root[0].is_imaginary() and not root[0].imag() == 0.0:
                    # Check to prevent conjugates creating additional kernels
                    if not root[0].conjugate() in [r[0] for r in roots_copy]:
                        continue

                    # If it doesn't exist then it's new so find and pop the complex conjugate of the current root
                    roots_copy.remove((root[0].conjugate(), root[1]))
                    roots_copy.remove(root)

                    # Create sinusoidal kernel
                    var("exponent_runner")
                    kernel_translation_kernel += globals()[f"signal_variance_{i}_{rootnum}"]**2*sum(t1**globals()["exponent_runner"] * t2**globals()["exponent_runner"], globals()["exponent_runner"], 0, root[1]-1) *\
                                                    exp(root[0].real()*(t1 + t2)) * cos(root[0].imag()*(t1-t2))
                else:
                    var("exponent_runner")
                    # Create the exponential kernel functions
                    kernel_translation_kernel += globals()[f"signal_variance_{i}_{rootnum}"]**2*sum(t1**globals()["exponent_runner"] * t2**globals()["exponent_runner"], globals()["exponent_runner"], 0, root[1]-1) * exp(root[0]*(t1+t2))
            translation_dictionary[f"LODEGP_kernel_{i}"] = kernel_translation_kernel 
        sage_covariance_matrix[i][i] = globals()[f"LODEGP_kernel_{i}"]
    return sage_covariance_matrix, translation_dictionary, param_dict


def build_dict_for_SR_expression(expression):
    final_dict = {}
    for coeff_dx1 in expression.coefficients(dx1):
        final_dict.update({(Integer(coeff_dx1[1]), Integer(coeff_dx2[1])): coeff_dx2[0] for coeff_dx2 in coeff_dx1[0].coefficients(dx2)})
    return final_dict

def differentiate_kernel_matrix(K, V, Vt, kernel_translation_dictionary):
    """
    This code takes the sage covariance matrix and differentiation matrices
    and returns a list of lists containing the results of the `compile` 
    commands that calculate the respective cov. fct. entry
    """
    sage_multiplication_kernel_matrix = matrix(K.base_ring(), len(K[0]), len(K[0]), (V*K*Vt))
    final_kernel_matrix = [[None for i in range(len(K[0]))] for j in range(len(K[0]))]
    for i, row in  enumerate(sage_multiplication_kernel_matrix):
        for j, cell in enumerate(row):
            cell_expression = 0
            diff_dictionary = build_dict_for_SR_expression(cell)
            for summand in diff_dictionary:
                #temp_cell_expression = mul([K[i][i] for i, multiplicant in enumerate(summand[3:]) if multiplicant > 0])
                temp_cell_expression = diff_dictionary[summand]
                for kernel_translation in kernel_translation_dictionary:
                    if kernel_translation in str(temp_cell_expression):
                        temp_cell_expression = SR(temp_cell_expression)
                        #cell = cell.factor()
                        #replace
                        temp_cell_expression = temp_cell_expression.substitute(globals()[kernel_translation]==kernel_translation_dictionary[kernel_translation])

                # And now that everything is replaced: diff that bad boy!
                cell_expression += SR(temp_cell_expression).diff(t1, summand[0]).diff(t2, summand[1])
            final_kernel_matrix[i][j] = cell_expression
    return final_kernel_matrix 


def replace_sum_and_diff(kernelmatrix, sumname="t_sum", diffname="t_diff", onesname="t_ones", zerosname="t_zeroes"):
    result_kernel_matrix = cp.deepcopy(kernelmatrix)
    var(sumname, diffname)
    for i, row in enumerate(kernelmatrix):
        for j, cell in enumerate(row):
            # Check if the cell is just a number
            if type(cell) == sage.symbolic.expression.Expression and not cell.is_numeric():
                #result_kernel_matrix[i][j] = cell.substitute({t1-t2:globals()[diffname], t1+t2:globals()[sumname]})
                result_kernel_matrix[i][j] = cell.substitute({t1:0.5*globals()[sumname] + 0.5*globals()[diffname], t2:0.5*globals()[sumname] - 0.5*globals()[diffname]})
            # This case is assumed to be just a constant, but we require it to be of 
            # the same size as the other covariance submatrices
            else:
                if cell == 0:
                    var(zerosname)
                    result_kernel_matrix[i][j] = globals()[zerosname]
                else:
                    var(onesname)
                    result_kernel_matrix[i][j] = cell * globals()[onesname]
    return result_kernel_matrix


def replace_basic_operations(kernel_string):
    # Define the regex replacement rules for the text
    regex_replacements_multi_group = {
        "exp" : [r'(e\^)\((([^()]*|\(([^()]*|\([^()]*\))*\))*)\)', "torch.exp"],
        "exp_singular" : [r'(e\^)([0-9a-zA-Z_]*)', "torch.exp"]
    }
    regex_replacements_single_group = {
        "sin" : [r'sin', "torch.sin"],
        "cos" : [r'cos', "torch.cos"],
        "pow" : [r'\^', "**"]
    }
    for replace_term in regex_replacements_multi_group:
        m = re.search(regex_replacements_multi_group[replace_term][0], kernel_string)
        if not m is None:
            # There is a second group, i.e. we have exp(something)
            kernel_string = re.sub(regex_replacements_multi_group[replace_term][0], f'{regex_replacements_multi_group[replace_term][1]}'+r"(\2)", kernel_string)
    for replace_term in regex_replacements_single_group:
        m = re.search(regex_replacements_single_group[replace_term][0], kernel_string)
        if not m is None:
            kernel_string = re.sub(regex_replacements_single_group[replace_term][0], f'{regex_replacements_single_group[replace_term][1]}', kernel_string)

    return kernel_string 


def replace_parameters(kernel_string, model_parameters, common_terms = []):
    regex_replace_string = r"(^|[\*\+\/\(\)\-\s])(REPLACE)([\*\+\/\(\)\-\s]|$)"
    
    for term in common_terms:
        if term in kernel_string:
            kernel_string = re.sub(regex_replace_string.replace("REPLACE", term), r"\1" + f"common_terms[\"{term}\"]" + r"\3", kernel_string)

    for model_param in model_parameters:
        kernel_string = re.sub(regex_replace_string.replace("REPLACE", model_param), r"\1"+f"torch.exp(model_parameters[\"{model_param}\"])"+r"\3", kernel_string)

    return kernel_string 


def verify_sage_entry(kernel_string, local_vars):
    # This is a call to willingly produce an error if the string is not originally coming from sage
    try:
        if type(kernel_string) == sage.symbolic.expression.Expression:
            kernel_string = kernel_string.simplify()
        kernel_string = str(kernel_string)
        sage_eval(kernel_string, locals = local_vars)
    except Exception as E:
        raise Exception(f"The string was not safe and has not been used to construct the Kernel.\nPlease ensure that only valid operations are part of the kernel and all variables have been declared.\nYour kernel string was:\n'{kernel_string}'")


def translate_kernel_matrix_to_gpytorch_kernel(kernelmatrix, paramdict, common_terms=[]):
    kernel_call_matrix = [[] for i in range(len(kernelmatrix))]
    for rownum, row in enumerate(kernelmatrix):
        for colnum, cell in enumerate(row):
            # First thing I do: Verify that the entry is a valid sage command
            local_vars = {str(v):v for v in SR(cell).variables()}
            verify_sage_entry(cell, local_vars)
            # Now translate the cell to a call
            replaced_op_cell = replace_basic_operations(str(cell))
            replaced_var_cell = replace_parameters(replaced_op_cell, paramdict, common_terms)
            #print("DEBUG: replaced_var_cell:")
            #print(replaced_var_cell)
            kernel_call_matrix[rownum].append(compile(replaced_var_cell, "", "eval"))



    return kernel_call_matrix



class Drastic_changepoint_Kernel(Kernel):
    '''
    This Kernel implements the drastic change in covariance as described by 
    R. Garnett, M. A. Osborne, and S. J. Roberts, “Sequential Bayesian prediction in the presence of changepoints,” 
    in Proceedings of the 26th Annual International Conference on Machine Learning, Montreal Quebec Canada: ACM, Jun. 2009, pp. 345-352. doi: 10.1145/1553374.1553418.

    '''
    def __init__(self, covar_modules:List[Kernel], changepoints:List[float], num_tasks:int, active_dims=None):
        super(Drastic_changepoint_Kernel, self).__init__(active_dims=active_dims)

        if len(covar_modules) != len(changepoints) + 1:
            raise ValueError("The number of changepoints must be one less than the number of covar_modules")
        
        self.covar_modules = covar_modules
        self.changepoints = changepoints
        self.num_tasks = num_tasks 

    def forward(self, x1, x2, diag=False, **params):
        #K = torch.zeros(x1.size(0) * self.num_tasks, x2.size(0)* self.num_tasks)

        K = torch.zeros(self.num_tasks, x1.size(0), self.num_tasks, x2.size(0))

        for i in range(x1.size(0)):
            for j in range(x2.size(0)):
                idx_1 = max([k for k in range(len(self.changepoints)) if self.changepoints[k] < x1[i]], default=-1) + 1
                idx_2 = max([k for k in range(len(self.changepoints)) if self.changepoints[k] < x2[j]], default=-1) + 1

                if idx_1 == idx_2:
                    K[:, i, :, j] = self.covar_modules[idx_1](x1[i], x2[j], diag=False, **params).evaluate()
                    #K[i*self.num_tasks:(i+1)*self.num_tasks, j*self.num_tasks:(j+1)*self.num_tasks] = self.covar_modules[idx_1](x1[i], x2[j], diag=diag, **params)
                    
                    #if idx_1 ==1:
                    #    print(f"idx_1: {idx_1}, idx_2: {idx_2}")
                    
                else:
                    K[:, i, :, j] = torch.zeros(self.num_tasks, self.num_tasks)
                    #print('this should not happen')
                    #K[i*self.num_tasks:(i+1)*self.num_tasks, j*self.num_tasks:(j+1)*self.num_tasks] = torch.zeros(self.num_tasks, self.num_tasks)
        
        K = K.view(x1.size(0)*self.num_tasks, x2.size(0)*self.num_tasks)
        return K
    
    def num_outputs_per_input(self, x1, x2):
            """
            Given `n` data points `x1` and `m` datapoints `x2`, this multitask
            kernel returns an `(n*num_tasks) x (m*num_tasks)` covariance matrix.
            """
            return self.num_tasks


class Param_LODE_Kernel(Kernel):
        def __init__(self, A, x0, common_terms, active_dims=None):
            super(Param_LODE_Kernel, self).__init__(active_dims=active_dims)

            self.model_parameters = torch.nn.ParameterDict()
            #self.num_tasks = num_tasks

            D, U, V = A.smith_form()
            print(f"D:{D}")
            print(f"V:{V}")
            x, a, b = var("x, a, b")
            V_temp = [list(b) for b in V.rows()]
            #print(V_temp)
            V = sage_eval(f"matrix({str(V_temp)})", locals={"x":x, "a":a, "b":b})
            Vt = V.transpose()
            kernel_matrix, self.kernel_translation_dict, parameter_dict = create_kernel_matrix_from_diagonal(D)

            # add equilibrium vars to the param dict
            parameter_dict["x1"] = torch.nn.Parameter(torch.tensor(x0[0]), requires_grad=False)
            parameter_dict["x2"] = torch.nn.Parameter(torch.tensor(x0[1]), requires_grad=False)
            parameter_dict["x3"] = torch.nn.Parameter(torch.tensor(x0[2]), requires_grad=False)
            parameter_dict["u"] = torch.nn.Parameter(torch.tensor(x0[3]), requires_grad=False)

            self.ode_count = A.nrows()
            self.kernelsize = len(kernel_matrix)
            self.model_parameters.update(parameter_dict)
            #print(self.model_parameters)
            x, dx1, dx2, t1, t2, *_ = var(["x", "dx1", "dx2"] + ["t1", "t2"] + [f"LODEGP_kernel_{i}" for i in range(len(kernel_matrix[Integer(0)]))])
            k = matrix(Integer(len(kernel_matrix)), Integer(len(kernel_matrix)), kernel_matrix)
            V = V.substitute(x=dx1)
            Vt = Vt.substitute(x=dx2)

            self.V = V
            self.matrix_multiplication = matrix(k.base_ring(), len(k[0]), len(k[0]), (V*k*Vt))
            self.diffed_kernel = differentiate_kernel_matrix(k, V, Vt, self.kernel_translation_dict)
            self.sum_diff_replaced = replace_sum_and_diff(self.diffed_kernel)
            self.covar_description = translate_kernel_matrix_to_gpytorch_kernel(self.sum_diff_replaced, self.model_parameters, common_terms=common_terms)

            self.num_tasks = len(self.covar_description)

        def num_outputs_per_input(self, x1, x2):
            """
            Given `n` data points `x1` and `m` datapoints `x2`, this multitask
            kernel returns an `(n*num_tasks) x (m*num_tasks)` covariance matrix.
            """
            return self.num_tasks

        #def forward(self, X, Z=None, common_terms=None):
        def forward(self, x1, x2, diag=False, **params):
            common_terms = params["common_terms"]
            model_parameters = self.model_parameters
            if not x2 is None:
                common_terms["t_diff"] = x1-x2.t()
                common_terms["t_sum"] = x1+x2.t()
                common_terms["t_ones"] = torch.ones_like(x1+x2.t())
                common_terms["t_zeroes"] = torch.zeros_like(x1+x2.t())
            K_list = list() 
            for rownum, row in enumerate(self.covar_description):
                for cell in row:
                    K_list.append(eval(cell))
            kernel_count = len(self.covar_description)
            # from https://discuss.pytorch.org/t/how-to-interleave-two-tensors-along-certain-dimension/11332/6
            #if K_list[0].ndim == 1:
            #    K_list = [kk.unsqueeze(1) for kk in K_list]
            K = einops.rearrange(K_list, '(t1 t2) h w -> (h t1) (w t2)', t1=kernel_count, t2=kernel_count)  

            return K 
        
        def __str__(self, substituted=False):
            if substituted:
                return pprint.pformat(str(self.sum_diff_replaced), indent=self.kernelsize)
            else:
                return pprint.pformat(str(self.diffed_kernel), indent=self.kernelsize)

        def __latexify_kernel__(self, substituted=False):
            if substituted:
                return pprint.pformat(latex(self.sum_diff_replaced), indent=self.kernelsize)
            else:
                return pprint.pformat(latex(self.diffed_kernel), indent=self.kernelsize)

        def __pretty_print_kernel__(self, substituted=False):
            if substituted:
                return pprint.pformat(pretty_print(self.matrix_multiplication), indent=self.kernelsize)
            else:
                pretty_print(self.matrix_multiplication)
                print(str(self.kernel_translation_dict))