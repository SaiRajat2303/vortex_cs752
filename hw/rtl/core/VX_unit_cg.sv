// Copyright © 2019-2023
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
// http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

`include "VX_define.vh"

// Reactive clock gating of one execution unit.
//
// The unit's clock runs while an instruction is being dispatched to it, while any
// instruction it accepted has not yet committed, or while the unit reports other
// pending work (busy_in). The in-flight count is per instruction: +1 on a dispatch
// packet with sop, -1 on a commit packet with eop, so gaps between the packets of
// one instruction never stop the clock early.
//
// The enable is combinational in the dispatch valid: VX_clockgate samples it while
// clk is low, so the edge that captures a new dispatch is passed and waking costs
// no cycle. The counter runs on the free clock.

module VX_unit_cg #(
    parameter `STRING INSTANCE_ID = "",
    parameter NUM_PORTS = 1,
    parameter CTR_W     = 10
) (
    input  wire                 clk,
    input  wire                 reset,

    input  wire [NUM_PORTS-1:0] in_valid,     // dispatch valid
    input  wire [NUM_PORTS-1:0] in_start,     // dispatch fire with sop
    input  wire [NUM_PORTS-1:0] out_end,      // commit fire with eop
    input  wire                 busy_in,      // unit-specific pending work

    output wire                 clk_out
);
`ifdef VX_CFG_UNIT_CG_ENABLE
    reg [CTR_W-1:0] inflight;

    wire [CTR_W-1:0] n_start = CTR_W'($countones(in_start));
    wire [CTR_W-1:0] n_end   = CTR_W'($countones(out_end));

    always @(posedge clk) begin
        if (reset) begin
            inflight <= '0;
        end else begin
            inflight <= inflight + n_start - n_end;
        end
    end

    `RUNTIME_ASSERT(reset || (inflight + n_start >= n_end),
        ("%t: *** %s clock-gate in-flight count underflow", $time, INSTANCE_ID))

    wire clk_en = reset || (| in_valid) || (inflight != '0) || busy_in;

    VX_clockgate icg (
        .clk_in  (clk),
        .en      (clk_en),
        .clk_out (clk_out)
    );

`ifdef SIMULATION
    reg [63:0] total_cycles, gated_cycles, wakes;
    reg        clk_en_r;
    always @(posedge clk) begin
        if (reset) begin
            total_cycles <= '0;
            gated_cycles <= '0;
            wakes        <= '0;
            clk_en_r     <= 1'b1;
        end else begin
            total_cycles <= total_cycles + 64'(1);
            gated_cycles <= gated_cycles + 64'(!clk_en);
            wakes        <= wakes + 64'(clk_en && ~clk_en_r);
            clk_en_r     <= clk_en;
        end
    end
    final begin
        $display("CG: %s gated=%0d total=%0d wakes=%0d", INSTANCE_ID, gated_cycles, total_cycles, wakes);
    end
`endif
`else
    `UNUSED_SPARAM (INSTANCE_ID)
    `UNUSED_PARAM (CTR_W)
    `UNUSED_VAR (reset)
    `UNUSED_VAR (in_valid)
    `UNUSED_VAR (in_start)
    `UNUSED_VAR (out_end)
    `UNUSED_VAR (busy_in)
    assign clk_out = clk;
`endif

endmodule
