from panel_flowdash import register_port_type

# A turbine table feeds any DataFrame input, but only a turbine table feeds an
# input typed "Turbines", and the population table only feeds "StatePopulation".
register_port_type("Turbines", subtype_of=["DataFrame"])
register_port_type("StatePopulation", subtype_of=["DataFrame"])
