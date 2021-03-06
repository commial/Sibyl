import struct
import logging

from miasm2.jitter.loader.elf import vm_load_elf
from miasm2.analysis.machine import Machine
from miasm2.jitter.csts import PAGE_READ, PAGE_WRITE, EXCEPT_ACCESS_VIOL, EXCEPT_DIV_BY_ZERO, EXCEPT_PRIV_INSN
from miasm2.core.bin_stream import bin_stream_vm
from miasm2.jitter.emulatedsymbexec import EmulatedSymbExec
import miasm2.expression.expression as m2_expr
from miasm2.ir.ir import AssignBlock
from miasm2.core.objc import CHandler

from sibyl.commons import objc_is_dereferenceable, expr_to_types
from sibyl.config import config


class EmulatedSymbExecWithModif(EmulatedSymbExec):

    def __init__(self, *args, **kwargs):
        super(EmulatedSymbExecWithModif, self).__init__(*args, **kwargs)
        self.modified_exprs = set()

    def apply_change(self, dst, src):
        self.modified_exprs.add(dst)
        super(EmulatedSymbExecWithModif, self).apply_change(dst, src)

    def emul_ir_blocks(self, *args, **kwargs):
        self.modified_exprs = set()
        addr = super(EmulatedSymbExecWithModif, self).emul_ir_blocks(*args, **kwargs)
        return addr

class ExtractRef(object):
    '''
    Class used to concolic run a snapshot and extract references to input
    '''

    def __init__(self, testcreator, replayed_snapshot):
        '''
        @testcreator: TestCreator instance with associated information
        @replayed_snapshot: snapshot to be used
        '''
        self.isFuncFound = False
        self.filename = testcreator.program
        self.learned_addr = testcreator.address
        self.snapshot = replayed_snapshot
        self.replayexception = []
        self.abicls = testcreator.abicls
        self.machine = Machine(testcreator.machine)
        self.ira = self.machine.ira()
        self.ptr_size = self.ira.sizeof_pointer()/8
        self.types = testcreator.types
        self.prototype = testcreator.prototype
        self.logger = testcreator.logger

    def use_snapshot(self, jitter):
        '''Initilize the VM with the snapshot informations'''
        for reg, value in self.snapshot.input_reg.iteritems():
            setattr(jitter.cpu, reg, value)

        # Set values for input memory
        for addr, mem in self.snapshot.in_memory.iteritems():
            assert mem.access != 0
            if not jitter.vm.is_mapped(addr, mem.size):
                jitter.vm.add_memory_page(addr, mem.access, mem.data)
            else:
                if jitter.vm.get_mem_access(addr) & 0b11 == mem.access & 0b11:
                    jitter.vm.set_mem(addr, mem.data)
                else:
                    # TODO memory page is already set but have not the
                    # same access right. However delete page does not
                    # exist
                    jitter.vm.set_mem(addr, mem.data)

    def compare_snapshot(self, jitter):
        '''Compare the expected result with the real one to determine if the function is recognize or not'''
        func_found = True

        for reg, value in self.snapshot.output_reg.iteritems():
            if value != getattr(jitter.cpu, reg):
                self.replayexception += ["output register %s wrong : %i expected, %i found" % (reg, value, getattr(jitter.cpu, reg))]
                func_found = False

        for addr, mem in self.snapshot.out_memory.iteritems():
            self.logger.debug("Check @%s, %s bytes: %r", hex(addr), hex(mem.size), mem.data[:0x10])
            if mem.data != jitter.vm.get_mem(addr, mem.size):
                self.replayexception += ["output memory wrong at 0x%x: %s expected, %s found" % (addr + offset, repr(mem.data), repr(jitter.vm.get_mem(addr + offset, mem.size)))]
                func_found = False

        return func_found

    def end_func(self, jitter):
        if jitter.vm.is_mapped(getattr(jitter.cpu, self.ira.ret_reg.name), 1):
            self.replayexception += ["return value might be a pointer"]

        self.isFuncFound = self.compare_snapshot(jitter)

        jitter.run = False
        return False

    def is_pointer(self, expr):
        """Return True if expr may be a pointer"""
        target_types = expr_to_types(self.c_handler, expr)

        return any(objc_is_dereferenceable(target_type)
                   for target_type in target_types)

    def callback(self, jitter):

        # Check previous state
        is_symbolic = lambda expr: (isinstance(expr, m2_expr.ExprMem) and
                                    not isinstance(expr.arg, m2_expr.ExprInt))


        # When it is possible, consider only elements modified in the last run
        # -> speed up to avoid browsing the whole memory
        to_consider = self.symb.modified_exprs

        for symbol in to_consider:
            # Do not consider PC
            if symbol == self.ira.pc:
                continue

            # Write to @NN[... argX ...]
            if is_symbolic(symbol):
                self.memories_write.add(symbol)

            # Read from ... @NN[... argX ...] ...
            symb_value = self.symb.eval_expr(symbol)
            to_replace = {}
            for expr in m2_expr.ExprAff(symbol,
                                        symb_value).get_r(mem_read=True):
                if is_symbolic(expr):
                    if isinstance(expr, m2_expr.ExprMem):
                        # Consider each byte individually
                        # Case: @32[X] with only @8[X+1] to replace
                        addr_expr = expr.arg
                        new_expr = []
                        consider = False
                        for offset in xrange(expr.size/8):
                            sub_expr = m2_expr.ExprMem(self.symb.expr_simp(addr_expr + m2_expr.ExprInt(offset, size=addr_expr.size)),
                                                       8)
                            if not self.is_pointer(sub_expr):
                                # Not a PTR, we have to replace with the real value
                                original_sub_expr = sub_expr.replace_expr(self.init_values)
                                new_expr.append(self.symb.eval_expr(original_sub_expr))
                                consider = True
                            else:
                                new_expr.append(sub_expr)

                        # Rebuild the corresponding expression
                        if consider:
                            assert len(new_expr) == expr.size / 8
                            to_replace[expr] = m2_expr.ExprCompose(*new_expr)

                    if expr not in self.memories_write:
                        # Do not consider memory already written during the run
                        self.memories_read.add(expr)

            # Replace with real value for non-pointer symbols
            if to_replace:
                symb_value = self.symb.expr_simp(symb_value.replace_expr(to_replace))
                if isinstance(symbol, m2_expr.ExprMem):
                    # Replace only in ptr (case to_replace: @[arg] = 8, expr:
                    # @[arg] = @[arg])
                    symbol = m2_expr.ExprMem(self.symb.expr_simp(symbol.arg.replace_expr(to_replace)),
                                      symbol.size)
                self.symb.apply_change(symbol, symb_value)

            # Check computed values against real ones
            # TODO idem memory
            if (isinstance(symbol, m2_expr.ExprId) and
                isinstance(symb_value, m2_expr.ExprInt)):
                if hasattr(jitter.cpu, symbol.name):
                    value = m2_expr.ExprInt(getattr(jitter.cpu, symbol.name),
                                            symbol.size)
                    assert value == self.symb.symbols[symbol]

        cur_addr = jitter.pc
        self.logger.debug("Current address: %s", hex(cur_addr))
        if cur_addr == 0x1337BEEF or cur_addr == self.return_addr:
            # End reached
            if self.logger.isEnabledFor(logging.DEBUG):
                print "In:"
                for x in self.memories_read:
                    print "\t%s (%s)" % (x,
                                         self.c_handler.expr_to_c(x),
                    )
                print "Out:"
                for x in self.memories_write:
                    print "\t%s (%s)" % (x,
                                         self.c_handler.expr_to_c(x),
                    )
            return True

        # Update state
        ## Reset cache structures
        self.mdis.job_done.clear()
        self.symb_ir.blocs.clear()

        ## Update current state
        asm_block = self.mdis.dis_bloc(cur_addr)
        irblocks = self.symb_ir.add_bloc(asm_block)

        self.symb.emul_ir_blocks(cur_addr)

        return True

    def prepare_symbexec(self, jitter, return_addr):
        # Activate callback on each instr
        jitter.jit.set_options(max_exec_per_call=1, jit_maxline=1)
        #jitter.jit.log_mn = True
        #jitter.jit.log_regs = True
        jitter.exec_cb = self.callback

        # Disassembler
        self.mdis = self.machine.dis_engine(bin_stream_vm(jitter.vm),
                                            lines_wd=1)

        # Symbexec engine
        ## Prepare the symbexec engine
        self.symb_ir = self.machine.ir()
        self.symb = EmulatedSymbExecWithModif(jitter.cpu, jitter.vm, self.symb_ir, {})
        self.symb.enable_emulated_simplifications()

        ## Update registers value
        self.symb.reset_regs()
        self.symb.update_engine_from_cpu()

        ## Load the memory as ExprMem
        self.symb.func_read = None
        self.symb.func_write = None
        for base_addr, mem_segment in jitter.vm.get_all_memory().iteritems():
            # Split into 8 bytes chunk for get_mem_overlapping
            for start in xrange(0, mem_segment["size"], 8):
                expr_mem = m2_expr.ExprMem(m2_expr.ExprInt(base_addr + start,
                                                           size=64),
                                           size=8*min(8, mem_segment["size"] - start))
                # Its initialisation, self.symb.apply_change is not necessary
                self.symb.symbols[expr_mem] = self.symb._func_read(expr_mem)

        ## Save the initial state
        self.symbols_init = self.symb.symbols.copy()

        ## Save the returning address
        self.return_addr = return_addr

        # Inject argument
        # TODO
        # TODO: use abicls
        abi_order = ["RDI", "RSI", "RDX", "RCX", "R8", "R9"]
        self.init_values = {}
        struct_expr_types = {}
        self.args_symbols = []
        for i, param_name in enumerate(self.prototype.args_order):
            cur_arg_abi = getattr(self.ira.arch.regs, abi_order[i])
            cur_arg = m2_expr.ExprId("arg%d_%s" % (i, param_name),
                                     size=cur_arg_abi.size)
            arg_type = self.prototype.args[param_name]
            if objc_is_dereferenceable(arg_type):
                # Convert the argument to symbol to track access based on it
                self.init_values[cur_arg] = self.symb.symbols[cur_arg_abi]
                self.symb.apply_change(cur_arg_abi, cur_arg)
            struct_expr_types[cur_arg.name] = arg_type
            self.args_symbols.append(cur_arg)

        # Init Expr <-> C conversion
        # Strict access is deliberately not enforced (example: memcpy(struct))
        self.c_handler = CHandler(self.types, struct_expr_types,
                                  enforce_strict_access=False)

        # Init output structures
        self.memories_read = set()
        self.memories_write = set()

    def build_references(self):
        """At the end of the execution,
        - Fill memories accesses
        - Prepare output structures

        Enrich the snapshot with outputs
        """

        memory_in = {}
        memory_out = {}

        # Get the resulting symbolic value
        # TODO use abi
        output_value = self.symb.symbols[self.symb.ir_arch.arch.regs.RAX]

        # Fill memory *out* (written)
        for expr in self.memories_write:
            # Eval the expression with the *output* state
            value = self.symb.eval_expr(expr)
            assert isinstance(value, m2_expr.ExprInt)
            memory_out[expr] = value

        # Fill memory *in* (read)
        saved_symbols = self.symb.symbols
        self.symb.symbols = self.symbols_init
        for expr in self.memories_read:
            # Eval the expression with the *input* state
            original_expr = expr.replace_expr(self.init_values)
            value = self.symb.eval_expr(original_expr)
            assert isinstance(value, m2_expr.ExprInt)
            memory_in[expr] = value
        self.symb.symbols = saved_symbols

        if self.logger.isEnabledFor(logging.DEBUG):
            print "In:"
            print memory_in
            print "Out:"
            print memory_out
            print "Final value:"
            print output_value

        self.snapshot.memory_in = AssignBlock(memory_in)
        self.snapshot.memory_out = AssignBlock(memory_out)
        self.snapshot.output_value = output_value
        self.snapshot.c_handler = self.c_handler
        self.snapshot.arguments_symbols = self.args_symbols

    def run(self):
        '''Main function that is in charge of running the test and return the result:
        true if the snapshot has recognized the function, false else.'''

        # TODO inherit from Replay
        jitter = self.machine.jitter(config.miasm_engine)

        vm_load_elf(jitter.vm, open(self.filename, "rb").read())

        # Init segment
        jitter.ir_arch.do_stk_segm = True
        jitter.ir_arch.do_ds_segm = True
        jitter.ir_arch.do_str_segm = True
        jitter.ir_arch.do_all_segm = True

        FS_0_ADDR = 0x7ff70000
        jitter.cpu.FS = 0x4
        jitter.cpu.set_segm_base(jitter.cpu.FS, FS_0_ADDR)
        jitter.vm.add_memory_page(
            FS_0_ADDR + 0x28, PAGE_READ, "\x42\x42\x42\x42\x42\x42\x42\x42", "Stack canary FS[0x28]")

        # Init the jitter with the snapshot
        self.use_snapshot(jitter)

        # Get the return address for our breakpoint
        return_addr = struct.unpack("P", jitter.vm.get_mem(jitter.cpu.RSP,
                                                           0x8))[0]
        jitter.add_breakpoint(return_addr, self.end_func)

        # Prepare the execution
        jitter.init_run(self.learned_addr)
        self.prepare_symbexec(jitter, return_addr)

        # Run the execution
        try:
            jitter.continue_run()
            assert jitter.run == False
        except AssertionError:
            if jitter.vm.get_exception() & EXCEPT_ACCESS_VIOL:
                self.replayexception += ["access violation"]
            elif jitter.vm.get_exception() & EXCEPT_DIV_BY_ZERO:
                self.replayexception += ["division by zero"]
            elif jitter.vm.get_exception() & EXCEPT_PRIV_INSN:
                self.replayexception += ["execution of private instruction"]
            elif jitter.vm.get_exception():
                self.replayexception += ["exception no %i" % (jitter.vm.get_exception())]
            else:
                raise
            self.isFuncFound = False

        # Rebuild references
        self.build_references()

        return self.isFuncFound
